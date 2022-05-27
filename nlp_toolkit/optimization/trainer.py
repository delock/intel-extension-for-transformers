import collections
import inspect
import math
from operator import length_hint
import numpy as np
import os
import copy
import sys
import time
import warnings
import torch
import torch.distributed as dist
from functools import partial
from neural_compressor.experimental import Component
from neural_compressor.utils import logger
from nlp_toolkit import (
    AutoDistillation,
    DistillationConfig,
    Provider,
    PruningMode,
    QuantizationConfig,
    QuantizationMode,
    PruningConfig
)
from nlp_toolkit.optimization.metrics import Metric
from packaging import version
from torch import nn
from torch.utils.data.dataloader import DataLoader
from torch.utils.data.dataset import IterableDataset
from torch.utils.data.distributed import DistributedSampler
from tqdm.auto import tqdm
from transformers import __version__, Trainer, PreTrainedModel
from transformers.configuration_utils import PretrainedConfig
from transformers.debug_utils import DebugOption, DebugUnderflowOverflow
from transformers.file_utils import (
    CONFIG_NAME,
    WEIGHTS_NAME,
    is_apex_available,
    is_datasets_available,
    is_torch_tpu_available,
    is_sagemaker_mp_enabled,
)
# Integrations must be imported before ML frameworks:
from transformers.integrations import hp_params
from transformers.modeling_utils import unwrap_model
from transformers.trainer import TRAINER_STATE_NAME, TRAINING_ARGS_NAME
from transformers.trainer_callback import TrainerState
from transformers.trainer_pt_utils import (
    IterableDatasetShard,
    find_batch_size,
    nested_numpify,
)
from transformers.trainer_utils import (
    HPSearchBackend,
    ShardedDDPOption,
    TrainOutput,
    EvalLoopOutput,
    EvalPrediction,
    get_last_checkpoint,
    set_seed,
    speed_metrics,
    denumpify_detensorize,
)
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING, Union


if is_datasets_available():
    import datasets

# pylint: disable=E0401
if is_torch_tpu_available():
    import torch_xla.core.xla_model as xm

# pylint: disable=E0401
if is_apex_available():
    from apex import amp

# pylint: disable=E0401
if is_sagemaker_mp_enabled():
    from .trainer_pt_utils import smp_forward_backward

if TYPE_CHECKING:
    import optuna


class NLPTrainer(Trainer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.in_training = False
        self._provider = "inc"
        self._eval_func = None
        self._train_func = None
        self.teacher_model = None
        self._calib_dataloader = None
        self._resuming_checkpoint = None
        self.compression_ctrl = None
        self.component = None
        self.enable_inc_quant = False
        self.pruner = None
        self.quantizer = None
        self.distiller = None
        self.fp32_model = None
        # This flag is set for the engine in the export_to_int8_onnx API.
        self.enable_executor = False

    @property
    def resuming_checkpoint(self):
        return self._resuming_checkpoint

    @resuming_checkpoint.setter
    def resuming_checkpoint(self, path: str):
        self._resuming_checkpoint = path

    @property
    def provider(self):
        return self._provider

    @property
    def eval_func(self):
        return self._eval_func

    @property
    def train_func(self):
        return self._train_func

    @property
    def calib_dataloader(self):
        return self._calib_dataloader

    @provider.setter
    def provider(self, prov: str):
        self._provider = prov

    @eval_func.setter
    def eval_func(self, func: Callable):
        self._eval_func = func

    @train_func.setter
    def train_func(self, func: Callable):
        self._train_func = func

    @calib_dataloader.setter
    def calib_dataloader(self, dataloader):
        self._calib_dataloader = dataloader

    def builtin_eval_func(self, model):
        self.model = model
        if self.args.seed:
            torch.manual_seed(self.args.seed)
        results = self.evaluate()
        logger.info(results)
        if isinstance(self.metrics, list):
            nums = len(self.metrics)
            for metric in self.metrics:
                assert metric.name in results.keys(), \
                    "Please set metric from {}".format(results.keys())
            if nums == 1:
                result = results.get(self.metrics[0].name)
            else:
                result = 0
                for metric in self.metrics:
                    assert metric.weight_ratio is not None, \
                        "Please set weights for metric if you want to use more than one metric"
                    result += results[metric.name] * metric.weighted
            logger.info("metric: {}".format(result))
        elif isinstance(self.metrics, Metric):
            assert self.metrics.name in results.keys(), \
                    "Please set metric from {}".format(results.keys())
            result = results.get(self.metrics.name)
            logger.info("metric: {}".format(result))
        else:
            assert False, "Please set the correct metrics format from the README"
        logger.info("Throughput: {} samples/sec".format(results.get("eval_samples_per_second")))
        return result

    def builtin_train_func(self, model):
        self.model_wrapped = model
        self.model = model
        train_result = self.train(component=self.component, resume_from_checkpoint=self._resuming_checkpoint)
        metrics = train_result.metrics
        self.save_model()  # Saves the tokenizer too for easy upload
        self.log_metrics("train", metrics)
        self.save_metrics("train", metrics)
        self.save_state()
        return self.model

    def init_quantizer(
        self,
        quant_config,
        provider: str = Provider.INC.value,
    ):
        from neural_compressor.experimental import Quantization, common

        assert isinstance(quant_config, QuantizationConfig), \
            "Please pass QuantizationConfig instance to trainer.quantize!"
        self.quant_config = quant_config
        self.metrics = self.quant_config.metrics
        self._provider = Provider[provider.upper()].value

        if self.quant_config.framework == "pytorch":
            if self.quant_config.approach == \
              QuantizationMode.POSTTRAININGDYNAMIC.value:
                self.quant_config.framework = "pytorch"
            else:
                self.quant_config.framework = "pytorch_fx"

        quantizer = Quantization(self.quant_config.inc_config)
        quantizer.model = common.Model(self.model)

        self.quantizer = quantizer
        return quantizer

    # pylint: disable=E0401
    def _nncf_quantize(self):
        from nlp_toolkit import NncfConfig
        from nncf import create_compressed_model
        compression_state = None
        assert isinstance(self.quant_config, NncfConfig), \
            "Please pass a NNCFConfig instance to trainer.quantize!"
        
        self.metrics = self.quant_config.metrics
        nncf_compression_state_file = self.quant_config.compression_state

        if os.path.isfile(nncf_compression_state_file):
            compression_state = torch.load(nncf_compression_state_file)
        else:
            compression_state = None

        compression_algo_controller, model = create_compressed_model(
            self.model, self.quant_config.nncf_config,
            compression_state=compression_state
        )

        self.compression_ctrl = \
            compression_algo_controller.distributed() \
            if self.quant_config.distributed else compression_algo_controller

        self.model = self._train_func(model)

    def _inc_quantize(
        self,
        quant_config,
        provider: str = Provider.INC.value,
    ):
        try:
            # we do deepcopy to keep the fp32 model for the export_to_int8_onnx API.
            self.fp32_model = copy.deepcopy(self.model)
        except Exception as e:   # pragma: no cover
            logger.warning("Model deepcopy failed: {}!".format(repr(e)))
        if self.quantizer is None:
            self.init_quantizer(quant_config=quant_config, provider=provider)
        if self._eval_func is not None:
            self.quantizer.eval_func = self._eval_func
        else:
            assert self.metrics is not None, \
                "Please pass the metrics to QuantizationConfig.metrics!"
            self.quantizer.eval_func = self.builtin_eval_func

        if self.quant_config.approach == QuantizationMode.POSTTRAININGSTATIC.value:
            self.quantizer.calib_dataloader = self.get_train_dataloader() \
                if self._calib_dataloader is None else self._calib_dataloader
        elif self.quant_config.approach == QuantizationMode.QUANTIZATIONAWARETRAINING.value:
            self.quantizer.q_func = \
                self.builtin_train_func if self._train_func is None else self._train_func
        self.component = self.quantizer
        self.opt_model = self.quantizer.fit()
        self.enable_inc_quant = True
        self._save_inc_int8(self.opt_model, self.args.output_dir)
        logger.info(
            "quantized model and configure file have saved to {}".format(self.args.output_dir)
        )
        return self.opt_model

    def quantize(
        self,
        quant_config: QuantizationConfig = None,
        provider: str = Provider.INC.value,
        eval_func: Optional[Callable] = None,
        train_func: Optional[Callable] = None,
        calib_dataloader=None,
    ):
        self._eval_func = self.builtin_eval_func if eval_func is None else eval_func
        self._train_func = self.builtin_train_func if train_func is None else train_func
        if calib_dataloader is not None:
            self._calib_dataloader = calib_dataloader

        if self.quantizer is None:
            self._provider = Provider[provider.upper()].value

        if self._provider == Provider.NNCF.value:
            return self._nncf_quantize()
        elif self._provider == Provider.INC.value:
            return self._inc_quantize(quant_config=quant_config, provider=provider)
        else:
            assert False, "Unsupport provider:{}".format(self._provider)

    def _save_inc_int8(self, opt_model, output_dir):
        self.model.config.architectures = [self.model.__class__.__name__]
        self.model.config.torch_dtype = "int8"
        self.model.config.save_pretrained(output_dir)
        weights_file = os.path.join(os.path.abspath(
          os.path.expanduser(output_dir)), WEIGHTS_NAME)
        torch.save(opt_model.quantized_state_dict(), weights_file)
        logger.info(
            "quantized model and configure file have saved to {}".format(weights_file)
        )

    def init_pruner(
        self,
        pruning_config = None,
        provider: str = Provider.INC.value,
    ):
        from neural_compressor.experimental import Pruning, common
        self.pruning_config = pruning_config
        self.metrics = self.pruning_config.metrics
        self._provider = Provider[provider.upper()].value

        assert isinstance(self.pruning_config, PruningConfig), \
            "please pass a instance of PruningConfig to trainer.prune!"

        pruning_start_epoch, pruning_end_epoch = self.pruning_config.epoch_range

        if pruning_start_epoch > self.args.num_train_epochs - 1:
            logger.warning(
                f"Pruning end epoch {pruning_start_epoch} is higher than "
                f"the total number of training epoch "
                f"{self.args.num_train_epochs}. No pruning will be applied."
            )

        if pruning_end_epoch > self.args.num_train_epochs - 1:
            logger.warning(
                f"Pruning end epoch {pruning_end_epoch} is higher than "
                f"the total number of training epoch "
                f"{self.args.num_train_epochs}. The target sparsity will not be reached."
            )

        pruner = Pruning(self.pruning_config.inc_config)
        pruner.model = common.Model(self.model)

        self.pruner = pruner
        return pruner

    def prune(
        self,
        pruning_config = None,
        provider: str = Provider.INC.value,
        eval_func: Optional[Callable] = None,
        train_func: Optional[Callable] = None,
    ):
        if self.pruner is None:
            self.init_pruner(pruning_config=pruning_config, provider=provider)
        if eval_func is not None:
            self._eval_func = eval_func
        if train_func is not None:
            self._train_func = train_func

        if self._eval_func is not None:
            self.pruner.eval_func = self._eval_func
        else:
            assert self.metrics is not None, "Please pass metrics to trainer.pruning.metrics!"
            assert self.pruning_config.pruner[0].prune_type == PruningMode.BASICMAGNITUDE.value, \
                "Please pass eval_func to trainer.eval_func"
            self.pruner.eval_func = self.builtin_eval_func

        if self._train_func is not None:
            self.pruner.pruning_func = self._train_func
        else:
            assert self.pruning_config.pruner[0].prune_type == PruningMode.BASICMAGNITUDE.value, \
                "Please pass train_func to trainer.train_func"
            self.pruner.pruning_func = self.builtin_train_func

        self.component = self.pruner
        self.opt_model = self.pruner.fit()

        return self.opt_model

    def init_distiller(
        self,
        distillation_config,
        teacher_model: Union[PreTrainedModel, torch.nn.Module],
        provider: str = Provider.INC.value,
    ):
        from neural_compressor.experimental import Distillation, common
        assert isinstance(distillation_config, DistillationConfig), \
            "please pass a instance of PruningConfig to trainer.prune!"
        self.distillation_config = distillation_config
        self._provider = Provider[provider.upper()].value
        self.metrics = self.distillation_config.metrics
        self.teacher_model = teacher_model

        distiller = Distillation(self.distillation_config.inc_config)
        distiller.model = common.Model(self.model)
        distiller.teacher_model = common.Model(self.teacher_model)

        self.distiller = distiller
        return distiller

    def distill(
        self,
        distillation_config,
        teacher_model: Union[PreTrainedModel, torch.nn.Module],
        provider: str = Provider.INC.value,
        eval_func: Optional[Callable] = None,
        train_func: Optional[Callable] = None,
    ):
        if self.distiller is None:
            self.init_distiller(
                distillation_config=distillation_config,
                teacher_model=teacher_model,
                provider=provider
            )
        if eval_func is not None:
            self._eval_func = eval_func
        if train_func is not None:
            self._train_func = train_func

        if self._eval_func is not None:
            self.distiller.eval_func = self._eval_func
        else:
            assert self.metrics is not None, \
                "Please pass metrics to trainer.distillation.metrics!"
            self.distiller.eval_func = self.builtin_eval_func

        self.distiller.train_func = \
            self.builtin_train_func if self._train_func is None else self._train_func
        self.distiller.create_criterion()
        self.component = self.distiller
        self.opt_model = self.distiller.fit()

        return self.opt_model

    def train(
        self,
        component: Optional[Component] = None,
        resume_from_checkpoint: Optional[Union[str, bool]] = None,
        trial: Union["optuna.Trial", Dict[str, Any]] = None,
        ignore_keys_for_eval: Optional[List[str]] = None,
        **kwargs,
    ):
        """
        Main training entry point.

        Args:
            component (:obj:`Component`, `optional`):
                Component object handling the training process.
            resume_from_checkpoint (:obj:`str` or :obj:`bool`, `optional`):
                If a :obj:`str`, local path to a saved checkpoint as saved by a previous instance of
                :class:`~transformers.Trainer`. If a :obj:`bool` and equals `True`, load the last checkpoint in
                `args.output_dir` as saved by a previous instance of :class:`~transformers.Trainer`. If present,
                training will resume from the model/optimizer/scheduler states loaded here.
            trial (:obj:`optuna.Trial` or :obj:`Dict[str, Any]`, `optional`):
                The trial run or the hyperparameter dictionary for hyperparameter search.
            ignore_keys_for_eval (:obj:`List[str]`, `optional`)
                A list of keys in the output of your model (if it is a dictionary) that should be ignored when
                gathering predictions for evaluation during the training.
            kwargs:
                Additional keyword arguments used to hide deprecated arguments
        """
        resume_from_checkpoint = None if not resume_from_checkpoint else resume_from_checkpoint

        # memory metrics - must set up as early as possible
        self._memory_tracker.start()

        args = self.args

        self.is_in_train = True

        self.component = component

        # do_train is not a reliable argument, as it might not be set and .train() still called, so
        # the following is a workaround:
        if args.fp16_full_eval and not args.do_train:
            self._move_model_to_device(self.model, args.device)

        if "model_path" in kwargs:
            resume_from_checkpoint = kwargs.pop("model_path")
            warnings.warn(
                "`model_path` is deprecated and will be removed in a future version. Use `resume_from_checkpoint` "
                "instead.",
                FutureWarning,
            )
        if len(kwargs) > 0:
            raise TypeError(f"train() received got unexpected keyword arguments: {', '.join(list(kwargs.keys()))}.")
        # This might change the seed so needs to run first.
        self._hp_search_setup(trial)

        # Model re-init
        model_reloaded = False
        if self.model_init is not None:
            # Seed must be set before instantiating the model when using model_init.
            set_seed(args.seed)
            self.model = self.call_model_init(trial)
            model_reloaded = True
            # Reinitializes optimizer and scheduler
            self.optimizer, self.lr_scheduler = None, None

        # Load potential model checkpoint
        if isinstance(resume_from_checkpoint, bool) and resume_from_checkpoint:
            resume_from_checkpoint = get_last_checkpoint(args.output_dir)
            if resume_from_checkpoint is None:
                raise ValueError(f"No valid checkpoint found in output directory ({args.output_dir})")

        if resume_from_checkpoint is not None:
            if version.parse(__version__) < version.parse("4.19"):
                if not os.path.isfile(os.path.join(resume_from_checkpoint, WEIGHTS_NAME)):
                    raise ValueError(f"Can't find a valid checkpoint at {resume_from_checkpoint}")

                logger.info(f"Loading model from {resume_from_checkpoint}).")

                if os.path.isfile(os.path.join(resume_from_checkpoint, CONFIG_NAME)):
                    config = PretrainedConfig.from_json_file(os.path.join(resume_from_checkpoint, CONFIG_NAME))
                    checkpoint_version = config.transformers_version
                    if checkpoint_version is not None and checkpoint_version != __version__:
                        logger.warn(
                            f"You are resuming training from a checkpoint trained with {checkpoint_version} of "
                            f"Transformers but your current version is {__version__}. "
                            "This is not recommended and could yield to errors or unwanted behaviors."
                        )

                # We load the model state dict on the CPU to avoid an OOM error.
                state_dict = torch.load(os.path.join(resume_from_checkpoint, WEIGHTS_NAME), map_location="cpu")
                # If the model is on the GPU, it still works!
                self._load_state_dict_in_model(state_dict)

                # release memory
                del state_dict
            else:
                self._load_from_checkpoint(resume_from_checkpoint)

        # If model was re-initialized, put it on the right device and update self.model_wrapped
        if model_reloaded:
            if self.place_model_on_device:
                self._move_model_to_device(self.model, args.device)
            self.model_wrapped = self.model

        # Keeping track whether we can can len() on the dataset or not
        train_dataset_is_sized = isinstance(self.train_dataset, collections.abc.Sized)

        # Data loader and number of training steps
        train_dataloader = self.get_train_dataloader()

        # Setting up training control variables:
        # number of training epochs: num_train_epochs
        # number of training steps per epoch: num_update_steps_per_epoch
        # total number of training steps to execute: max_steps
        total_train_batch_size = args.train_batch_size * args.gradient_accumulation_steps * args.world_size
        if train_dataset_is_sized:
            num_update_steps_per_epoch = len(train_dataloader) // args.gradient_accumulation_steps
            num_update_steps_per_epoch = max(num_update_steps_per_epoch, 1)
            if args.max_steps > 0:
                max_steps = args.max_steps
                num_train_epochs = args.max_steps // num_update_steps_per_epoch + int(
                    args.max_steps % num_update_steps_per_epoch > 0
                )
                # May be slightly incorrect if the last batch in the training datalaoder has a smaller size but it's
                # the best we can do.
                num_train_samples = args.max_steps * total_train_batch_size
            else:
                max_steps = math.ceil(args.num_train_epochs * num_update_steps_per_epoch)
                num_train_epochs = math.ceil(args.num_train_epochs)
                num_train_samples = len(self.train_dataset) * args.num_train_epochs
        else:
            # see __init__. max_steps is set when the dataset has no __len__
            max_steps = args.max_steps
            # Setting a very large number of epochs so we go as many times as necessary over the iterator.
            num_train_epochs = sys.maxsize
            num_update_steps_per_epoch = max_steps
            num_train_samples = args.max_steps * total_train_batch_size

        if DebugOption.UNDERFLOW_OVERFLOW in self.args.debug:
            if self.args.n_gpu > 1:
                # nn.DataParallel(model) replicates the model, creating new variables and module
                # references registered here no longer work on other gpus, breaking the module
                raise ValueError(
                    "Currently --debug underflow_overflow is not supported under DP. "
                    "Please use DDP (torch.distributed.launch)."
                )
            else:
                debug_overflow = DebugUnderflowOverflow(self.model)  # noqa

        delay_optimizer_creation = self.sharded_ddp is not None and self.sharded_ddp != ShardedDDPOption.SIMPLE

        if not delay_optimizer_creation:
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        self.state = TrainerState()
        self.state.is_hyper_param_search = trial is not None

        # Activate gradient checkpointing if needed
        if args.gradient_checkpointing:
            self.model.gradient_checkpointing_enable()

        model = self._wrap_model(self.model_wrapped)

        # for the rest of this function `model` is the outside model, whether it was wrapped or not
        if model is not self.model:
            self.model_wrapped = model

        if delay_optimizer_creation:
            self.create_optimizer_and_scheduler(num_training_steps=max_steps)

        # Check if saved optimizer or scheduler states exist
        self._load_optimizer_and_scheduler(resume_from_checkpoint)

        # important: at this point:
        # self.model         is the Transformers Model
        # self.model_wrapped is DDP(Transformers Model), Deepspeed(Transformers Model), etc.

        # Train!
        num_examples = (
            self.num_examples(train_dataloader) if train_dataset_is_sized else total_train_batch_size * args.max_steps
        )

        logger.info("***** Running training *****")
        logger.info(f"  Num examples = {num_examples}")
        logger.info(f"  Num Epochs = {num_train_epochs}")
        logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
        logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_train_batch_size}")
        logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
        logger.info(f"  Total optimization steps = {max_steps}")

        self.state.epoch = 0
        start_time = time.time()
        epochs_trained = 0
        steps_trained_in_current_epoch = 0
        steps_trained_progress_bar = None

        # Check if continuing training from a checkpoint
        if resume_from_checkpoint is not None and os.path.isfile(
            os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME)
        ):
            self.state = TrainerState.load_from_json(os.path.join(resume_from_checkpoint, TRAINER_STATE_NAME))
            epochs_trained = self.state.global_step // num_update_steps_per_epoch
            if not args.ignore_data_skip:
                steps_trained_in_current_epoch = self.state.global_step % (num_update_steps_per_epoch)
                steps_trained_in_current_epoch *= args.gradient_accumulation_steps
            else:
                steps_trained_in_current_epoch = 0

            logger.info("  Continuing training from checkpoint, will skip to saved global_step")
            logger.info(f"  Continuing training from epoch {epochs_trained}")
            logger.info(f"  Continuing training from global step {self.state.global_step}")
            if not args.ignore_data_skip:
                logger.info(
                    f"  Will skip the first {epochs_trained} epochs then the first {steps_trained_in_current_epoch} "
                    "batches in the first epoch. If this takes a lot of time, you can add the `--ignore_data_skip` "
                    "flag to your launch command, but you will resume the training on data already seen by your model."
                )
                if self.is_local_process_zero() and not args.disable_tqdm:
                    steps_trained_progress_bar = tqdm(total=steps_trained_in_current_epoch)
                    steps_trained_progress_bar.set_description("Skipping the first batches")

        # Update the references
        self.callback_handler.model = self.model
        self.callback_handler.optimizer = self.optimizer
        self.callback_handler.lr_scheduler = self.lr_scheduler
        self.callback_handler.train_dataloader = train_dataloader
        self.state.trial_name = self.hp_name(trial) if self.hp_name is not None else None
        if trial is not None:
            assignments = trial.assignments if self.hp_search_backend == HPSearchBackend.SIGOPT else trial
            self.state.trial_params = hp_params(assignments)
        else:
            self.state.trial_params = None
        # This should be the same if the state has been saved but in case the training arguments changed, it's safer
        # to set this after the load.
        self.state.max_steps = max_steps
        self.state.num_train_epochs = num_train_epochs
        self.state.is_local_process_zero = self.is_local_process_zero()
        self.state.is_world_process_zero = self.is_world_process_zero()

        tr_loss = torch.tensor(0.0).to(args.device)
        # _total_loss_scalar is updated everytime .item() has to be called on tr_loss and stores the sum of all losses
        self._total_loss_scalar = 0.0
        self._globalstep_last_logged = self.state.global_step
        model.zero_grad()

        self.control = self.callback_handler.on_train_begin(args, self.state, self.control)

        # Skip the first epochs_trained epochs to get the random state of the dataloader at the right point.
        if not args.ignore_data_skip:
            for epoch in range(epochs_trained):
                # We just need to begin an iteration to create the randomization of the sampler.
                for _ in train_dataloader:
                    break
        if isinstance(component, Component):
            if hasattr(self.component, "teacher_model"):
                self.component.teacher_model._model = self._wrap_model(self.component.teacher_model.model)
            component.pre_epoch_begin()
            if component.combination is not None and "Quantization" in component.combination:
                model = component.model.model
        for epoch in range(epochs_trained, num_train_epochs):
            if self.compression_ctrl is not None:
                self.compression_ctrl.scheduler.epoch_step()
                print(self.compression_ctrl.statistics().to_str())
            if isinstance(train_dataloader, DataLoader) and isinstance(train_dataloader.sampler, DistributedSampler):
                train_dataloader.sampler.set_epoch(epoch)
            elif isinstance(train_dataloader.dataset, IterableDatasetShard):
                train_dataloader.dataset.set_epoch(epoch)

            epoch_iterator = train_dataloader

            # Reset the past mems state at the beginning of each epoch if necessary.
            if args.past_index >= 0:
                self._past = None

            steps_in_epoch = (
                len(epoch_iterator) if train_dataset_is_sized else args.max_steps * args.gradient_accumulation_steps
            )
            self.control = self.callback_handler.on_epoch_begin(args, self.state, self.control)
            if isinstance(component, Component):
                component.on_epoch_begin(epoch)

            self.in_training = True
            for step, inputs in enumerate(epoch_iterator):

                # Skip past any already trained steps if resuming training
                if steps_trained_in_current_epoch > 0:
                    steps_trained_in_current_epoch -= 1
                    if steps_trained_progress_bar is not None:
                        steps_trained_progress_bar.update(1)
                    if steps_trained_in_current_epoch == 0:
                        self._load_rng_state(resume_from_checkpoint)
                    continue
                elif steps_trained_progress_bar is not None:
                    steps_trained_progress_bar.close()
                    steps_trained_progress_bar = None

                if step % args.gradient_accumulation_steps == 0:
                    self.control = self.callback_handler.on_step_begin(args, self.state, self.control)
                    if isinstance(component, Component):
                        component.on_batch_begin(step)

                if (
                    ((step + 1) % args.gradient_accumulation_steps != 0)
                    and args.local_rank != -1
                    and args._no_sync_in_gradient_accumulation
                ):
                    # Avoid unnecessary DDP synchronization since there will be no backward pass on this example.
                    with model.no_sync():
                        tr_loss_step = self.training_step(model, inputs)
                else:
                    tr_loss_step = self.training_step(model, inputs)

                if args.logging_nan_inf_filter and (torch.isnan(tr_loss_step) or torch.isinf(tr_loss_step)):
                    # if loss is nan or inf simply add the average of previous logged losses
                    tr_loss += tr_loss / (1 + self.state.global_step - self._globalstep_last_logged)
                else:
                    tr_loss += tr_loss_step

                self.current_flos += float(self.floating_point_ops(inputs))

                if (step + 1) % args.gradient_accumulation_steps == 0 or (
                    # last step in epoch but step is always smaller than gradient_accumulation_steps
                    steps_in_epoch <= args.gradient_accumulation_steps
                    and (step + 1) == steps_in_epoch
                ):
                    if isinstance(component, Component):
                        component.on_post_grad()

                    # Gradient clipping
                    if args.max_grad_norm is not None and args.max_grad_norm > 0:

                        if hasattr(self.optimizer, "clip_grad_norm"):
                            # Some optimizers (like the sharded optimizer) have a specific way to do gradient clipping
                            self.optimizer.clip_grad_norm(args.max_grad_norm)
                        elif hasattr(model, "clip_grad_norm_"):
                            # Some models (like FullyShardedDDP) have a specific way to do gradient clipping
                            model.clip_grad_norm_(args.max_grad_norm)
                        else:
                            # Revert to normal clipping otherwise, handling Apex or full precision
                            nn.utils.clip_grad_norm_(
                                model.parameters(),
                                args.max_grad_norm,
                            )

                    # Optimizer step
                    if self.compression_ctrl is not None:
                        self.compression_ctrl.scheduler.step()
                    optimizer_was_run = True
                    self.optimizer.step()

                    if optimizer_was_run:
                        self.lr_scheduler.step()

                    model.zero_grad()
                    self.state.global_step += 1
                    self.state.epoch = epoch + (step + 1) / steps_in_epoch
                    self.state.curr_loss = tr_loss_step.cpu().detach().item()
                    self.control = self.callback_handler.on_step_end(args, self.state, self.control)
                    if isinstance(component, Component):
                        component.on_batch_end()
                    self._maybe_log_save_evaluate(tr_loss, model, trial, epoch, ignore_keys_for_eval)
                else:
                    self.control = self.callback_handler.on_substep_end(args, self.state, self.control)

                if self.control.should_epoch_stop or self.control.should_training_stop:
                    break

            self.in_training = False
            self.control = self.callback_handler.on_epoch_end(args, self.state, self.control)
            if isinstance(component, Component):
                # When Distillation is involved, model will be evaluated in "on_epoch_end" hook, while in SQuAD 
                # evaluation, "start_positions" and "end_positions" will be removed from inputs of the fx model,
                # this will damage the training afterward, so use the copied model for evaluation, 
                # and then restore the model.
                component.model.model = copy.deepcopy(model)
                component.on_epoch_end()
                component.model.model = model
                if 'Distillation' in component.__repr__():
                    model.train()
            self._maybe_log_save_evaluate(tr_loss, model, trial, epoch, ignore_keys_for_eval)

            if DebugOption.TPU_METRICS_DEBUG in self.args.debug:
                logger.warning(
                    "You enabled PyTorch/XLA debug metrics but you don't have a TPU "
                    "configured. Check your training configuration if this is unexpected."
                )

            if self.control.should_training_stop:
                break

        if isinstance(component, Component):
            component.post_epoch_end()
            if component.combination is not None and "Quantization" in component.combination:
                self.model = component.model.model

        if args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of training
            delattr(self, "_past")

        logger.info("\n\nTraining completed. Do not forget to share your model on huggingface.co/models =)\n\n")
        if args.load_best_model_at_end and self.state.best_model_checkpoint is not None:
            # Wait for everyone to get here so we are sur the model has been saved by process 0.
            if args.local_rank != -1:
                dist.barrier()

            if version.parse(__version__) < version.parse("4.19"):
                logger.info(
                    f"Loading best model from {self.state.best_model_checkpoint} (score: {self.state.best_metric})."
                )

                best_model_path = os.path.join(self.state.best_model_checkpoint, WEIGHTS_NAME)
                if os.path.exists(best_model_path):
                    # We load the model state dict on the CPU to avoid an OOM error.
                    state_dict = torch.load(best_model_path, map_location="cpu")
                    # If the model is on the GPU, it still works!
                    self._load_state_dict_in_model(state_dict)
                else:
                    logger.warn(
                        f"Could not locate the best model at {best_model_path}, "
                        "if you are running a distributed training on multiple nodes, "
                        "you should activate `--save_on_each_node`."
                    )
            else:
                self._load_best_model()

        # add remaining tr_loss
        self._total_loss_scalar += tr_loss.item()
        train_loss = self._total_loss_scalar / self.state.global_step

        metrics = speed_metrics("train", start_time, num_samples=num_train_samples, num_steps=self.state.max_steps)
        self.store_flos()
        metrics["total_flos"] = self.state.total_flos
        metrics["train_loss"] = train_loss

        self.is_in_train = False

        self._memory_tracker.stop_and_update_metrics(metrics)

        self.log(metrics)

        self.control = self.callback_handler.on_train_end(args, self.state, self.control)

        return TrainOutput(self.state.global_step, train_loss, metrics)

    def _maybe_log_save_evaluate(self, tr_loss, model, trial, epoch, ignore_keys_for_eval):
        if self.control.should_log:
            if is_torch_tpu_available():
                xm.mark_step()

            logs: Dict[str, float] = {}

            # all_gather + mean() to get average loss over all processes
            tr_loss_scalar = self._nested_gather(tr_loss).mean().item()

            # reset tr_loss to zero
            tr_loss -= tr_loss

            logs["loss"] = round(tr_loss_scalar / (self.state.global_step - self._globalstep_last_logged), 4)
            logs["learning_rate"] = self._get_learning_rate()

            # pylint: disable=E0401
            if self.compression_ctrl is not None:
                from nncf.common.utils.tensorboard import prepare_for_tensorboard
                logs["compression_loss"] = self.compression_ctrl.loss().item()
                compression_stats = self.compression_ctrl.statistics()
                for key, value in prepare_for_tensorboard(compression_stats).items():
                    logs["compression/statistics/{0}".format(key)] = value
                print(compression_stats.to_str())

            self._total_loss_scalar += tr_loss_scalar
            self._globalstep_last_logged = self.state.global_step
            self.store_flos()

            self.log(logs)

        metrics = None
        if self.control.should_evaluate:
            metrics = self.evaluate(ignore_keys=ignore_keys_for_eval)
            self._report_to_hp_search(trial, epoch, metrics)

        if self.control.should_save:
            self._save_checkpoint(model, trial, metrics=metrics)
            self.control = self.callback_handler.on_save(self.args, self.state, self.control)

    def training_step(self, model: nn.Module, inputs: Dict[str, Union[torch.Tensor, Any]]) -> torch.Tensor:
        """
        Perform a training step on a batch of inputs.
        Subclass and override to inject custom behavior.
        Args:
            model (:obj:`nn.Module`):
                The model to train.
            inputs (:obj:`Dict[str, Union[torch.Tensor, Any]]`):
                The inputs and targets of the model.
                The dictionary will be unpacked before being fed to the model. Most models expect the targets under the
                argument :obj:`labels`. Check your model's documentation for all accepted arguments.
        Return:
            :obj:`torch.Tensor`: The tensor with training loss on this batch.
        """
        model.train()
        inputs = self._prepare_inputs(inputs)

        if is_sagemaker_mp_enabled():
            scaler = self.scaler if self.use_amp else None
            loss_mb = smp_forward_backward(model, inputs, self.args.gradient_accumulation_steps, scaler=scaler)
            return loss_mb.reduce_mean().detach().to(self.args.device)

        # pylint: disable=E0401
        if self.use_amp:
            from torch.cuda.amp import autocast
            with autocast():
                loss = self.compute_loss(model, inputs)
        else:
            loss = self.compute_loss(model, inputs)

        if self.args.n_gpu > 1:
            loss = loss.mean()  # mean() to average on multi-gpu parallel training

        if self.args.gradient_accumulation_steps > 1 and not self.deepspeed:
            # deepspeed handles loss scaling by gradient_accumulation_steps in its `backward`
            loss = loss / self.args.gradient_accumulation_steps

        if self.compression_ctrl is not None:
            compression_loss = self.compression_ctrl.loss()
            loss += compression_loss

        if self.use_amp:
            self.scaler.scale(loss).backward()
        elif self.use_apex:
            with amp.scale_loss(loss, self.optimizer) as scaled_loss:
                scaled_loss.backward()
        elif self.deepspeed:
            # loss gets scaled under gradient_accumulation_steps in deepspeed
            loss = self.deepspeed.backward(loss)
        else:
            loss.backward()

        return loss.detach()

    def compute_loss(self, model, inputs, return_outputs=False):
        """
        How the loss is computed by Trainer. By default, all models return the loss in the first element.

        Subclass and override for custom behavior.
        """
        if self.label_smoother is not None and "labels" in inputs:
            labels = inputs.pop("labels")
        else:
            labels = None
        if "teacher_logits" in inputs:
            teacher_logits = inputs.pop("teacher_logits")
            if "start_positions" in inputs and "end_positions" in inputs: # for SQuAD
                teacher_logits = torch.vstack(list(teacher_logits))
        else:
            teacher_logits = None

        outputs = model(**inputs)
        if self.in_training and hasattr(self, "component") and \
           hasattr(self.component, "criterion"):
            qa_output_merger = lambda outputs : torch.vstack([torch.vstack([sl, el]) for sl, el in \
                                                zip(outputs["start_logits"], outputs["end_logits"])])
            qa_output_spliter = lambda outputs : (outputs[0::2], outputs[1::2])
            def get_logits(outputs):
                if isinstance(outputs, dict):
                    if "logits" in outputs:
                        logits = outputs["logits"]
                    elif "start_logits" in outputs and "end_logits" in outputs:
                        logits = qa_output_merger(outputs)
                    elif "prediction_logits" in outputs:
                        logits = outputs["prediction_logits"]
                    else:
                        raise AssertionError("Logits of outputs not included, can't compute loss")
                elif isinstance(outputs, torch.Tensor):
                    logits = outputs
                else:
                    logits = outputs[1]
                return logits

            if labels is None:
                if "labels" in inputs: # for GLUE
                    labels = inputs["labels"]
                elif "start_positions" in inputs and "end_positions" in inputs: # for SQuAD
                    labels = torch.hstack([torch.tensor([sp, ep]) for sp, ep in \
                            zip(inputs["start_positions"], inputs["end_positions"])])
                else:
                    raise AssertionError("Labels of input data not provided, can't compute loss")
            logits = get_logits(outputs)
            if hasattr(self.component, "on_post_forward"):
                self.component.on_post_forward(inputs, teacher_output=teacher_logits)
                if hasattr(self.component.criterion, "teacher_outputs"):
                    self.component.criterion.teacher_outputs = \
                        get_logits(self.component.criterion.teacher_outputs)
            loss = self.component.criterion(logits, labels)
            if hasattr(self.component.criterion, 'add_origin_loss') and \
                self.component.criterion.add_origin_loss:
                loss = loss + outputs['loss']
            if "start_positions" in inputs and "end_positions" in inputs:
                start_logits, end_logits = qa_output_spliter(logits)
                outputs = {"start_logits":start_logits, "end_logits":end_logits, "loss":loss}
            else:
                outputs = {"logits":logits, "loss":loss}
        else:
            # Save past state if it exists
            # TODO: this needs to be fixed and made cleaner later.
            if self.args.past_index >= 0:
                self._past = outputs[self.args.past_index]

            if labels is not None:
                loss = self.label_smoother(outputs, labels)
            else:
                # We don't use .loss here since the model may return tuples instead of ModelOutput.
                loss = outputs["loss"] if isinstance(outputs, dict) else outputs[0]

        return (loss, outputs) if return_outputs else loss

    def _remove_unused_columns(self, dataset: "datasets.Dataset", description: Optional[str] = None):
        if not self.args.remove_unused_columns:
            return dataset
        if self._signature_columns is None:
            # Inspect model forward signature to keep only the arguments it accepts.
            if self._provider == "nncf":
                signature = inspect.signature(self.model.get_nncf_wrapped_model().forward)
            else:
                signature = inspect.signature(self.model.forward)
            self._signature_columns = list(signature.parameters.keys())
            # Labels may be named label or label_ids, the default data collator handles that.
            self._signature_columns += ["label", "label_ids", "teacher_logits"]
        columns = [k for k in self._signature_columns if k in dataset.column_names]
        ignored_columns = list(set(dataset.column_names) - set(self._signature_columns))
        if len(ignored_columns) > 0:
            dset_description = "" if description is None else f"in the {description} set "
            logger.info(
                f"The following columns {dset_description} don't have a corresponding argument in "
                f"`{self.model.__class__.__name__}.forward` and have been ignored: {', '.join(ignored_columns)}."
            )

        if version.parse(datasets.__version__) < version.parse("1.4.0"):
            dataset.set_format(
                type=dataset.format["type"], columns=columns, format_kwargs=dataset.format["format_kwargs"]
            )
            return dataset
        else:
            return dataset.remove_columns(ignored_columns)

    def autodistillation(
        self,
        autodistillation_config,
        teacher_model: Union[PreTrainedModel, torch.nn.Module],
        provider: str = Provider.INC.value,
        model_builder: Optional[Callable] = None,
        model_cls: Optional[Callable] = None,
        eval_func: Optional[Callable] = None,
        train_func: Optional[Callable] = None,
    ):
        self.autodistillation_config = autodistillation_config
        self._provider = Provider[provider.upper()].value
        self.evaluation_loop = self.auto_distil_evaluation_loop

        if model_builder is None:
            assert model_cls is not None, "Must specify model_cls to use the built-in " + \
                "model_builder, e.g. model_cls=AutoModelForPreTraining, or you can use " + \
                "the customized model_builder."
            model_builder = partial(self.model_builder_builtin, model_cls=model_cls)
        agent = AutoDistillation(model_builder, self.autodistillation_config)
        self.args.lr_scheduler_type = 'constant'

        def take_train_steps(model, trainer, agent=None, train_steps=None,
                             block_name=None, checkpoint=None):
            trainer.model_wrapped = model
            trainer.model = model
            if train_steps is not None and isinstance(train_steps, int):
                trainer.args.max_steps = train_steps
            if block_name is not None and isinstance(block_name, str):
                for name, para in model.named_parameters():
                    if block_name in name:
                        para.requires_grad = True
                    else:
                        para.requires_grad = False
            train_result = trainer.train(agent, resume_from_checkpoint=checkpoint)
            metrics = train_result.metrics
            trainer.save_model()  # Saves the tokenizer too for easy upload
            trainer.log_metrics("train", metrics)
            trainer.save_metrics("train", metrics)
            trainer.save_state()
            return trainer.model

        def take_eval_steps(model, trainer, metric_names, save_metrics=False):
            trainer.model = model
            metrics = trainer.evaluate()
            if save_metrics:
                trainer.save_metrics("eval", metrics)
            metrics['latency'] = 1000.0 / metrics.get("eval_samples_per_second")
            metric_names = ['eval_loss', 'latency'] if metric_names is None else metric_names
            for metric_name in metric_names:
                logger.info("{}: {}".format(metric_name, metrics.get(metric_name)))
            logger.info(
                "Throughput: {} samples/sec".format(metrics.get("eval_samples_per_second"))
                )
            return {metric_name: metrics.get(metric_name) for metric_name in metric_names}

        def train_func_builtin(model):
            from torch.utils.data import Subset
            def run_distillers(model, distillers, train_steps, block_names,
                               checkpoints=None, presentation='flash distillation'):
                max_train_steps=0
                begin_time = time.time()
                if checkpoints is None:
                    checkpoints = [None] * len(distillers)
                for i, elements in \
                    enumerate(zip(distillers, train_steps, block_names, checkpoints)):
                    start_time = time.time()
                    distiller, ts, bln, checkpoint = elements
                    logger.info(
                        ' '.join(['='*30, 'Step {} of'.format(i+1), presentation, '='*30]))
                    distiller.student_model = model
                    distiller.teacher_model = teacher_model
                    distiller.criterion = None # force creating new criterion object
                    distiller.create_criterion()
                    if checkpoint is None:
                        max_train_steps = 0
                    max_train_steps += ts
                    distiller.train_func = \
                        partial(take_train_steps, trainer=self, agent=distiller, 
                                train_steps=max_train_steps, block_name=bln, 
                                checkpoint=checkpoint)
                    # distiller.eval_func = \
                    #     partial(take_eval_steps, trainer=self, metric_name='eval_loss')
                    # shuffle train_dataset before each training
                    indices = list(range(len(self.train_dataset)))
                    np.random.shuffle(indices)
                    self.train_dataset = Subset(self.train_dataset, indices)
                    model = distiller().model
                    logger.info(
                        ' '.join(['='*30, 'Step {} of'.format(i+1), presentation,
                            'consumed {:.2f} min'.format((time.time()-start_time) / 60), '='*30])
                        )
                logger.info(
                    ' '.join(['='*30, presentation,
                        'consumed {:.2f} h'.format((time.time()-begin_time) / 3600), '='*30])
                    )
                return model
            
            self.optimizer, self.lr_scheduler = None, None
            self._move_model_to_device(teacher_model, self.args.device)
            self._move_model_to_device(model, self.args.device)
            # create new distillers before each train process
            agent.create_distillers()
            # run flash_distillers
            model = run_distillers(model, 
                                   agent.flash_distillers, 
                                   agent.flash_train_steps, 
                                   agent.flash_block_names)
            # run regular_distillers
            model = run_distillers(model, 
                                   agent.regular_distillers, 
                                   agent.regular_train_steps, 
                                   agent.regular_block_names,
                                   presentation='regular distillation')
            return model

        def eval_func_builtin(model):
            return take_eval_steps(model, trainer=self, 
                                   metric_names=agent.metrics, 
                                   save_metrics=True)

        agent.train_func = train_func \
            if train_func else train_func_builtin
        agent.eval_func = eval_func \
            if eval_func else eval_func_builtin
        return agent.search_loop(self.args.output_dir)
    
    def model_builder_builtin(self, arch_paras=None, model_cls=None):
        config = self.model.config
        if arch_paras is not None:
            assert isinstance(arch_paras, dict), "Expect arch_paras to be a dict."
            for k in arch_paras:
                if hasattr(config, k):
                    config.__setattr__(k, arch_paras[k])
                    # for MobileBERT, 'intra_bottleneck_size' is associated with 
                    # 'true_hidden_size', and must have the same values.
                    if k == 'intra_bottleneck_size':
                        config.__setattr__('true_hidden_size', arch_paras[k])
        return model_cls.from_config(config)

    def auto_distil_evaluation_loop(
        self,
        dataloader: DataLoader,
        description: str,
        prediction_loss_only: Optional[bool] = None,
        ignore_keys: Optional[List[str]] = None,
        metric_key_prefix: str = "eval",
    ) -> EvalLoopOutput:
        """
        Prediction/evaluation loop, shared by :obj:`Trainer.evaluate()` and :obj:`Trainer.predict()`.

        Works both with or without labels.
        Does not save all predictions and labels to avoid out of memory when predictions is huge.
        """
        prediction_loss_only = (
            prediction_loss_only if prediction_loss_only is not None else self.args.prediction_loss_only
        )

        model = self._wrap_model(self.model, training=False)

        # if full fp16 is wanted on eval and this ``evaluation`` or ``predict`` isn't called while
        # ``train`` is running, halve it first and then put on device
        if not self.is_in_train and self.args.fp16_full_eval:
            model = model.half().to(self.args.device)

        batch_size = dataloader.batch_size

        logger.info(f"***** Running {description} *****")
        if isinstance(dataloader.dataset, collections.abc.Sized):
            logger.info(f"  Num examples = {self.num_examples(dataloader)}")
        else:
            logger.info("  Num examples: Unknown")
        logger.info(f"  Batch size = {batch_size}")

        model.eval()

        self.callback_handler.eval_dataloader = dataloader
        # Do this before wrapping.
        eval_dataset = dataloader.dataset

        if self.args.past_index >= 0:
            self._past = None

        # Initialize containers
        # losses/preds/labels on GPU/TPU (accumulated for eval_accumulation_steps)
        losses_host = None
        # losses/preds/labels on CPU (final containers)
        all_losses = None
        # Will be useful when we have an iterable dataset so don't know its length.

        observed_num_examples = 0
        all_metrics = {}
        # Main evaluation loop
        for step, inputs in enumerate(dataloader):
            # Update the observed num examples
            observed_batch_size = find_batch_size(inputs)
            if observed_batch_size is not None:
                observed_num_examples += observed_batch_size
                # For batch samplers, batch_size is not known by the dataloader in advance.
                if batch_size is None:
                    batch_size = observed_batch_size

            # Prediction step
            loss, logits, labels = self.prediction_step(model, inputs, prediction_loss_only, ignore_keys=ignore_keys)

            # Update containers on host
            if loss is not None:
                losses = self._nested_gather(loss.repeat(batch_size))
                losses_host = losses if losses_host is None else torch.cat((losses_host, losses), dim=0)
            if logits is not None:
                logits = self._pad_across_processes(logits)
                logits = self._nested_gather(logits)
            if labels is not None:
                labels = self._pad_across_processes(labels)
                labels = self._nested_gather(labels)
            self.control = self.callback_handler.on_prediction_step(self.args, self.state, self.control)
            if self.compute_metrics is not None and logits is not None and labels is not None:
                metrics = self.compute_metrics(EvalPrediction(predictions=nested_numpify(logits), 
                                                              label_ids=nested_numpify(labels)))
                if not all_metrics:
                    all_metrics = metrics
                else:
                    assert all_metrics.keys() == metrics.keys(), \
                        'Different keys between all_metrics and metrics, {} vs. {}.'.format(
                            metrics.keys(), all_metrics.keys())
                    for k in metrics.keys():
                        all_metrics[k] += metrics[k]

            # Gather all tensors and put them back on the CPU if we have done enough accumulation steps.
            if self.args.eval_accumulation_steps is not None and (step + 1) % self.args.eval_accumulation_steps == 0:
                if losses_host is not None:
                    losses = nested_numpify(losses_host)
                    all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)

                # Set back to None to begin a new accumulation
                losses_host = None
                # losses_host, preds_host, labels_host = None, None, None

        if self.args.past_index and hasattr(self, "_past"):
            # Clean the state at the end of the evaluation loop
            delattr(self, "_past")

        # Gather all remaining tensors and put them back on the CPU
        if losses_host is not None:
            losses = nested_numpify(losses_host)
            all_losses = losses if all_losses is None else np.concatenate((all_losses, losses), axis=0)

        # Number of samples
        if not isinstance(eval_dataset, IterableDataset):
            num_samples = len(eval_dataset)
        # The instance check is weird and does not actually check for the type, but whether the dataset has the right
        # methods. Therefore we need to make sure it also has the attribute.
        elif isinstance(eval_dataset, IterableDatasetShard) and hasattr(eval_dataset, "num_examples"):
            num_samples = eval_dataset.num_examples
        else:
            num_samples = observed_num_examples

        # Number of losses has been rounded to a multiple of batch_size and in a distributed training, the number of
        # samplers has been rounded to a multiple of batch_size, so we truncate.
        if all_losses is not None:
            all_losses = all_losses[:num_samples]

        if not all_metrics:
            metrics = {}
        else:
            metrics = {k: all_metrics[k] / (step + 1) for k in all_metrics}

        # To be JSON-serializable, we need to remove numpy types or zero-d tensors
        metrics = denumpify_detensorize(metrics)
        if all_losses is not None:
            metrics[f"{metric_key_prefix}_loss"] = all_losses.mean().item()

        # Prefix all keys with metric_key_prefix + '_'
        for key in list(metrics.keys()):
            if not key.startswith(f"{metric_key_prefix}_"):
                metrics[f"{metric_key_prefix}_{key}"] = metrics.pop(key)

        return EvalLoopOutput(predictions=logits, label_ids=labels, metrics=metrics, num_samples=num_samples)

    def _save(self, output_dir: Optional[str] = None, state_dict=None):
        # If we are executing this function, we are the process zero, so we don't check for that.
        output_dir = output_dir if output_dir is not None else self.args.output_dir
        os.makedirs(output_dir, exist_ok=True)
        logger.info(f"Saving model checkpoint to {output_dir}")
        # Save a trained model and configuration using `save_pretrained()`.
        # They can then be reloaded using `from_pretrained()`
        if not isinstance(self.model, PreTrainedModel):
            unwrapped_model = unwrap_model(self.model)
            if self._provider == "nncf":
                is_pretrained = isinstance(unwrapped_model.get_nncf_wrapped_model(), PreTrainedModel)
            else:
                is_pretrained = isinstance(unwrapped_model, PreTrainedModel)

            if is_pretrained:
                if state_dict is None:
                    state_dict = unwrapped_model.state_dict()
                unwrapped_model.save_pretrained(output_dir, state_dict=state_dict)
            else:
                logger.info("Trainer.model is not a `PreTrainedModel`, only saving its state dict.")
                if state_dict is None:
                    state_dict = self.model.state_dict()
                torch.save(state_dict, os.path.join(output_dir, WEIGHTS_NAME))
        else:
            # overwrite `pytorch_model.bin` with inc int8 format.
            if self.enable_inc_quant:
                self._save_inc_int8(self.opt_model, output_dir)
            else:
                self.model.save_pretrained(output_dir, state_dict=state_dict)
        if self.tokenizer is not None:
            self.tokenizer.save_pretrained(output_dir)

        # Good practice: save your training arguments together with the trained model
        torch.save(self.args, os.path.join(output_dir, TRAINING_ARGS_NAME))

    def export_to_onnx(self, *args, **kwargs):
        if not self.enable_inc_quant:
            self.export_to_fp32_onnx(*args, **kwargs)
        else:
            self.export_to_int8_onnx(*args, **kwargs)

    def export_to_fp32_onnx(self, 
            save_path=None, 
            opset_version=14, 
            do_constant_folding=True,
            verbose=True,
            ):
        if self.fp32_model is None:
            model = self.model.eval()
        else:
            # Quantized model cannot be converted into onnx
            model = self.fp32_model.eval()
        onnx_save_path = save_path if save_path \
          else os.path.join(self.args.output_dir, 'fp32-model.onnx')

        # Prepare input data
        eval_dataloader = self.get_eval_dataloader()
        it = iter(eval_dataloader)
        input = next(it)
        self._remove_label(input)
        # Set variable length axes
        symbolic_names = {0: 'batch_size', 1: 'max_seq_len'}
        axes_dict = {k: symbolic_names for k in input.keys()}

        import torch
        torch.onnx.export(
            model,
            tuple(input.values()),
            onnx_save_path,
            opset_version=opset_version,
            input_names=list(input.keys()),
            dynamic_axes=axes_dict,
            do_constant_folding=do_constant_folding,
        )
        if verbose:
            info = "ONNX Model exported to path: {0}".format(onnx_save_path)
            logger.info("*"*len(info))
            logger.info(info)
            logger.info("*"*len(info))

    def export_to_int8_onnx(self,
            save_path=None,
            quant_format='QDQ',
            dtype='U8U8',
            opset_version=14,
            opt_level='all',
            ):
        if self.provider != 'inc':   # pragma: no cover
            logger.error("export_to_onnx API only supports INC model right now.")
            sys.exit(0)

        if self.enable_executor:
            # Will deprecate after engine supports QDQ format and other op_types.
            op_types_to_quantize=['MatMul']
            pytorch_op_types_to_quantize=['Linear']
            addition_op_to_quantize = []
            opset_version = 11
            quant_format='Qlinear'
            logger.info("Engine only support opset_version=11 " + 
                        "and int8 MatMul.")
        else:
            from onnxruntime.quantization.registry import (
                IntegerOpsRegistry,
                QLinearOpsRegistry,
                QDQRegistry,
            )
            if 'dynamic' in self.opt_model.tune_cfg['approach']:
                op_types_to_quantize=['MatMul', 'Gather', "LSTM", 'Conv']
                pytorch_op_types_to_quantize=['Linear', 'Embedding', "LSTM", 
                                              'Conv1d', 'Conv2d']
                addition_op_to_quantize = list(IntegerOpsRegistry.keys())
            else:
                op_types_to_quantize=['MatMul', 'Gather', 'Conv']
                pytorch_op_types_to_quantize=['Linear', 'Embedding', 'Conv1d', 'Conv2d']
                if quant_format == 'QDQ':
                    addition_op_to_quantize = list(QDQRegistry.keys())
                    addition_op_to_quantize.remove('Relu') # ValueError: x not in list
                else:
                    addition_op_to_quantize = list(QLinearOpsRegistry.keys())

        if quant_format == 'QDQ' and opset_version < 13:
            opset_version = 14
            logger.error("Per-Channel support with QDQ format " + 
                        "requires onnx opset version 13 or above. " +
                        "Here we use opset_version=", opset_version)
        all_op_types_to_quantize = op_types_to_quantize + addition_op_to_quantize

        import onnx
        fp32_path = save_path + '.tmp' if save_path \
          else os.path.join(self.args.output_dir, 'int8-model.onnx.tmp')
        onnx_save_path = save_path if save_path \
          else os.path.join(self.args.output_dir, 'int8-model.onnx')
        self.export_to_fp32_onnx(fp32_path, opset_version=opset_version, 
                                 do_constant_folding=False, verbose=False)
        model = onnx.load(fp32_path)
        model = self._replace_gemm_with_matmul(model)
        onnx.save(model, fp32_path)

        # Get weight name from onnx initializer
        weight_name_list = []
        for tensor in model.graph.initializer:
            weight_name_list.append(tensor.name)

        # Match weight name with onnx node name
        quantize_nodes = []
        tmp_node_mapping = {}
        module_node_mapping = {}
        for node in model.graph.node:
            if node.op_type not in op_types_to_quantize:
                for inp in node.input:
                    if inp in weight_name_list and 'weight' in inp:
                        tmp_node_mapping.update({node.output[0] : inp.split('.weight')[0]})
                    elif inp in tmp_node_mapping:
                        tmp_node_mapping.update({node.output[0] : tmp_node_mapping[inp]})
            else:
                for inp in node.input:
                    if inp in weight_name_list and 'weight' in inp:
                        module_node_mapping.update({inp.split('.weight')[0] : node.name})
                    elif inp in tmp_node_mapping:
                        module_node_mapping.update({tmp_node_mapping[inp]: node.name})

            # Save all quantizable node name
            if node.op_type in all_op_types_to_quantize:
                quantize_nodes.append(node.name)

        # Match pytorch module name with onnx node name
        for k, v in self.opt_model.tune_cfg['op'].items():
            if k[1] not in pytorch_op_types_to_quantize or 'int8' in v['weight']['dtype']:
                continue
            if k[0] not in module_node_mapping:
                k[0] = k[0].split('.module')[0]
            if k[0] in module_node_mapping:
                fallback_op = module_node_mapping[k[0]]
                quantize_nodes.remove(fallback_op)

        # Quantization
        from onnxruntime.quantization import quantize_static, quantize_dynamic 
        from onnxruntime.quantization import QuantFormat, QuantType
        quant_format = QuantFormat.QOperator if quant_format != 'QDQ' else QuantFormat.QDQ

        if 'U8' in dtype:
            weight_type=QuantType.QUInt8,
            activation_type=QuantType.QUInt8,
        elif 'S8' in dtype:
            weight_type=QuantType.QInt8,
            activation_type=QuantType.QInt8,
        else:
            # Gather requires weight type be the same as activation.
            # So U8S8(acitvation|weight) option is not workable for best performance.
            logger.error("Right now, we don't support dtype: {}".format(dtype))
            sys.exit(0)

        if 'dynamic' in self.opt_model.tune_cfg['approach']:
            quantize_dynamic(fp32_path,
                            onnx_save_path,
                            per_channel=True,
                            weight_type=QuantType.QUInt8,
                            nodes_to_quantize=quantize_nodes,
                            nodes_to_exclude=[],
                            #op_types_to_quantize=op_types_to_quantize,
                            extra_options={})
        else:
            from onnxruntime.quantization import CalibrationDataReader
            class NLPDataReader(CalibrationDataReader):
                def __init__(self, dataloader, sample_size=100):
                    import math
                    self.dataloader = dataloader
                    self.batch_size = dataloader.batch_size
                    self.batch_num = math.ceil(sample_size/self.batch_size)
                    self.datasize = self.batch_num*self.batch_size

                    self.data = []
                    for i, batch in enumerate(self.dataloader):
                        if i * self.batch_size >= self.datasize:
                            break
                        NLPTrainer._remove_label(batch)
                        batch = {k: v.detach().cpu().numpy() for k, v in batch.items()}
                        self.data.append(batch)
                    self.data = iter(self.data)

                def get_next(self):
                    return next(self.data, None)

            calib_datareader = NLPDataReader(self.get_eval_dataloader())

            quantize_static(fp32_path,
                            onnx_save_path,
                            calib_datareader,
                            quant_format=quant_format,
                            per_channel=True,
                            weight_type=weight_type,
                            activation_type=activation_type,
                            nodes_to_quantize=quantize_nodes,
                            nodes_to_exclude=[],
                            #op_types_to_quantize=op_types_to_quantize,
                            extra_options={})

        os.remove(fp32_path)
        # Post-optimization
        import onnx
        import onnxruntime as rt
        model = onnx.load(onnx_save_path)
        sess_options = rt.SessionOptions()
        # Set graph optimization level
        if opt_level == 'all':
            opt_level_type = rt.GraphOptimizationLevel.ORT_ENABLE_ALL 
        elif opt_level == 'extend':
            opt_level_type = rt.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
        elif opt_level == 'basic':
            opt_level_type = rt.GraphOptimizationLevel.ORT_ENABLE_BASIC
        else:
            opt_level_type = rt.GraphOptimizationLevel.ORT_DISABLE_ALL

        sess_options.graph_optimization_level = opt_level_type
        sess_options.optimized_model_filepath = onnx_save_path
        rt.InferenceSession(onnx_save_path, sess_options)
        info = "ONNX Model exported to path: {0}".format(onnx_save_path)
        logger.info("*"*len(info))
        logger.info(info)
        logger.info("*"*len(info))

    def export_to_jit(self):
        self.model.eval()
        eval_dataloader = self.get_eval_dataloader()
        it = iter(eval_dataloader)
        input = next(it)
        self._remove_label(input)
        jit_model = torch.jit.trace(
            self.model,
            tuple(input.values()),
            strict=False
        )
        info = "JIT Model exported"
        logger.info("*"*len(info))
        logger.info(info)
        logger.info("*"*len(info))
        return jit_model

    # will remove after next INC(1.12) release
    def _replace_gemm_with_matmul(self, model):
        new_nodes = []
        import onnx
        from onnx import numpy_helper
        from neural_compressor.model.onnx_model import ONNXModel
        if not isinstance(model, ONNXModel):
            model = ONNXModel(model)

        for node in model.nodes():
            if node.op_type == 'Gemm':
                alpha = 1.0
                beta = 1.0
                transA = 0
                transB = 0
                for attr in node.attribute:
                    if attr.name == 'alpha':
                        alpha = onnx.helper.get_attribute_value(attr)
                    elif attr.name == 'beta':
                        beta = onnx.helper.get_attribute_value(attr)
                    elif attr.name == 'transA':
                        transA = onnx.helper.get_attribute_value(attr)
                    elif attr.name == 'transB':
                        transB = onnx.helper.get_attribute_value(attr)
                if alpha == 1.0 and beta == 1.0 and transA == 0:
                    inputB = node.input[1]
                    if transB == 1:
                        B = model.get_initializer(node.input[1])
                        if B:
                            # assume B is not used by any other node
                            B_array = numpy_helper.to_array(B)
                            B_trans = numpy_helper.from_array(B_array.T)
                            B_trans.name = B.name
                            model.remove_initializer(B)
                            model.add_initializer(B_trans)

                            #TBD this is for onnx model zoo, which are all in old IR version
                            if model.model.ir_version < 4:
                                for input in model.model.graph.input:
                                    if input.name == B_trans.name:
                                        for i, dim in enumerate(input.type.tensor_type.shape.dim):
                                            dim.dim_value = B_array.T.shape[i]

                        else:
                            inputB += '_Transposed'
                            transpose_node = onnx.helper.make_node('Transpose',
                                                                inputs=[node.input[1]],
                                                                outputs=[inputB],
                                                                name=node.name+'_Transpose')
                            new_nodes.append(transpose_node)

                    matmul_node = onnx.helper.make_node('MatMul',
                            inputs=[node.input[0], inputB],
                            outputs=[node.output[0] + ('_MatMul' if len(node.input)>2 else '')],
                            name=node.name + '_MatMul')
                    new_nodes.append(matmul_node)

                    if len(node.input) > 2:
                        add_node = onnx.helper.make_node('Add',
                            inputs=[node.output[0] + '_MatMul', node.input[2]],
                            outputs=node.output,
                            name=node.name + '_Add')
                        new_nodes.append(add_node)

                # unsupported
                else:
                    new_nodes.append(node)

            # not GEMM
            else:
                new_nodes.append(node)

        model.graph().ClearField('node')
        model.graph().node.extend(new_nodes)

        return model.model
    
    @staticmethod
    def _remove_label(input):
        if "labels" in input: # for GLUE
            input.pop('labels')
        elif "start_positions" in input and "end_positions" in input: # for SQuAD
            input.pop('start_positions')
            input.pop('end_positions')
        return input