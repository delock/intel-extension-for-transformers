import copy
import mlflow
import numpy as np
import os
import shutil
import torch.utils.data as data
import unittest
from datasets import load_dataset, load_metric
from nlp_toolkit import (
    DistillationConfig,
    DistillationCriterionMode,
    metrics,
    NLPTrainer,
    OptimizedModel,
)
from nlp_toolkit.optimization.distillation import Criterion
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer
)

os.environ["WANDB_DISABLED"] = "true"


class TestDistillation(unittest.TestCase):
    @classmethod
    def setUpClass(self):
        self.model = AutoModelForSequenceClassification.from_pretrained(
            'distilbert-base-uncased'
        )
        self.teacher_model = AutoModelForSequenceClassification.from_pretrained(
            'distilbert-base-uncased-finetuned-sst-2-english'
        )
        raw_datasets = load_dataset("glue", "sst2")["validation"]
        tokenizer = AutoTokenizer.from_pretrained("distilbert-base-uncased")
        def preprocess_function(examples):
            # Tokenize the texts
            args = (
                (examples['sentence'],)
            )
            result = tokenizer(*args, padding=True, max_length=64, truncation=True)
            return result
        raw_datasets = raw_datasets.map(
            preprocess_function, batched=True, load_from_cache_file=True
        )
        eval_dataset = raw_datasets.select(range(30))
        self.dataset = eval_dataset

    @classmethod
    def tearDownClass(self):
        shutil.rmtree('./tmp_trainer', ignore_errors=True)
        shutil.rmtree('./distilled_model', ignore_errors=True)

    def test_fx_model_distil(self):
        metric = load_metric("accuracy")
        def compute_metrics(p):
            preds = p.predictions
            preds = np.argmax(preds, axis=1)
            return metric.compute(predictions=preds, references=p.label_ids)
        origin_weight = copy.deepcopy(self.model.classifier.weight)
        for mode in DistillationCriterionMode:
            print("Distillation approach:", mode.value)
            self.trainer = NLPTrainer(
                model=copy.deepcopy(self.model),
                train_dataset=self.dataset,
                eval_dataset=self.dataset,
                compute_metrics=compute_metrics,
            )
            metric_ = metrics.Metric(name="eval_accuracy")
            criterion = Criterion(
                name='IntermediateLayersLoss',
                layer_mappings=[['classifier', 'classifier']],
                loss_types=['MSE'],
                loss_weight_ratio=[1.0],
                add_origin_loss=False
            ) if mode.value == "IntermediateLayersKnowledgeDistillationLoss" else None
            distillation_conf = DistillationConfig(metrics=metric_, criterion=criterion)
            distilled_model = self.trainer.distill(
                distillation_config=distillation_conf, teacher_model=self.teacher_model
            )
            # By default, model will be saved in tmp_trainer dir.
            self.trainer.save_model('./distilled_model')
            loaded_model = OptimizedModel.from_pretrained(
                './distilled_model',
            )
            distilled_weight = copy.deepcopy(distilled_model.model.classifier.weight)
            loaded_weight = copy.deepcopy(loaded_model.classifier.weight)
            # check distilled model
            self.assertTrue((distilled_weight != origin_weight).any())
            # check loaded model
            self.assertTrue((distilled_weight == loaded_weight).all())
            mlflow.end_run()


if __name__ == "__main__":
    unittest.main()