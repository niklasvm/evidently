from typing import List
from typing import Optional
from typing import Union

import dataclasses
import numpy as np
import pandas as pd
from sklearn.metrics import classification_report
from sklearn.metrics import log_loss
from sklearn.metrics import precision_score
from sklearn.metrics import recall_score

from evidently.calculations.classification_performance import DatasetClassificationQuality
from evidently.calculations.classification_performance import PredictionData
from evidently.calculations.classification_performance import calculate_matrix
from evidently.calculations.classification_performance import calculate_metrics
from evidently.calculations.classification_performance import k_probability_threshold
from evidently.metrics.base_metric import InputData
from evidently.metrics.classification_performance.base_classification_metric import ThresholdClassificationMetric
from evidently.metrics.classification_performance.classification_quality_metric import ClassificationQualityMetric
from evidently.model.widget import BaseWidgetInfo
from evidently.renderers.base_renderer import MetricRenderer
from evidently.renderers.base_renderer import default_renderer
from evidently.renderers.html_widgets import header_text
from evidently.renderers.html_widgets import table_data
from evidently.utils.data_operations import process_columns


@dataclasses.dataclass
class ClassificationDummyMetricResults:
    dummy: DatasetClassificationQuality
    by_reference_dummy: Optional[DatasetClassificationQuality]
    model_quality: Optional[DatasetClassificationQuality]
    metrics_matrix: dict


class ClassificationDummyMetric(ThresholdClassificationMetric[ClassificationDummyMetricResults]):
    quality_metric: ClassificationQualityMetric

    def __init__(
        self,
        threshold: Optional[float] = None,
        k: Optional[Union[float, int]] = None,
    ):
        super().__init__(threshold, k)
        self.threshold = threshold
        self.k = k
        self.quality_metric = ClassificationQualityMetric()

    def calculate(self, data: InputData) -> ClassificationDummyMetricResults:
        quality_metric: Optional[ClassificationQualityMetric]
        dataset_columns = process_columns(data.current_data, data.column_mapping)
        target_name = dataset_columns.utility_columns.target
        prediction_name = dataset_columns.utility_columns.prediction

        if target_name is None:
            raise ValueError("The column 'target' should present")
        if prediction_name is None:
            quality_metric = None
        else:
            quality_metric = self.quality_metric

        #  dummy by current
        labels_ratio = data.current_data[target_name].value_counts(normalize=True)
        np.random.seed(0)
        dummy_preds = np.random.choice(labels_ratio.index, data.current_data.shape[0], p=labels_ratio)
        dummy_preds = pd.Series(dummy_preds)
        prediction: Optional[PredictionData] = None
        if prediction_name is not None:
            target, prediction = self.get_target_prediction_data(data.current_data, data.column_mapping)
            labels = prediction.labels
        else:
            target = data.current_data[target_name]
            labels = list(target.unique())

        current_matrix = calculate_matrix(
            target,
            dummy_preds,
            labels,
        )
        current_dummy = calculate_metrics(
            data.column_mapping,
            current_matrix,
            target,
            PredictionData(predictions=dummy_preds, prediction_probas=None, labels=labels),
        )

        metrics_matrix = classification_report(
            target,
            dummy_preds,
            output_dict=True,
        )

        if prediction is not None and prediction.prediction_probas is not None and len(labels) == 2:
            if self.threshold is not None or self.k is not None:
                if self.threshold is not None:
                    threshold = self.threshold
                if self.k is not None:
                    threshold = k_probability_threshold(prediction.prediction_probas, self.k)
            else:
                threshold = 0.5
            current_dummy = self.correction_for_threshold(
                current_dummy, threshold, target, labels, prediction.prediction_probas.shape
            )
            # metrix matrix
            # neg label data
            if threshold == 1.0:
                coeff_recall = 1.0
            else:
                coeff_recall = min(1.0, 0.5 / (1 - threshold))
            coeff_precision = min(1.0, (1 - threshold) / 0.5)
            neg_label_precision = precision_score(target, dummy_preds, pos_label=labels[1]) * coeff_precision
            neg_label_recall = recall_score(target, dummy_preds, pos_label=labels[1]) * coeff_recall
            metrics_matrix = {
                str(labels[0]): {
                    "precision": current_dummy.precision,
                    "recall": current_dummy.recall,
                    "f1-score": current_dummy.f1,
                },
                str(labels[1]): {
                    "precision": neg_label_precision,
                    "recall": neg_label_recall,
                    "f1-score": 2 * neg_label_precision * neg_label_recall / (neg_label_precision + neg_label_recall),
                },
            }
        if prediction is not None and prediction.prediction_probas is not None:
            # dummy log_loss and roc_auc
            binaraized_target = (target.astype(str).values.reshape(-1, 1) == list(labels)).astype(int)
            dummy_prediction = np.full(prediction.prediction_probas.shape, 1 / prediction.prediction_probas.shape[1])
            current_dummy.log_loss = log_loss(binaraized_target, dummy_prediction)
            current_dummy.roc_auc = 0.5

        # dummy by reference
        by_reference_dummy: Optional[DatasetClassificationQuality] = None
        if data.reference_data is not None:
            labels_ratio = data.reference_data[target_name].value_counts(normalize=True)
            np.random.seed(1)
            dummy_preds = np.random.choice(labels_ratio.index, data.current_data.shape[0], p=labels_ratio)
            dummy_preds = pd.Series(dummy_preds)

            if prediction_name is not None:
                target, prediction = self.get_target_prediction_data(data.current_data, data.column_mapping)
                labels = prediction.labels
            else:
                target = data.current_data[target_name]
                labels = list(target.unique())

            current_matrix = calculate_matrix(
                target,
                dummy_preds,
                labels,
            )
            by_reference_dummy = calculate_metrics(
                data.column_mapping,
                current_matrix,
                target,
                PredictionData(predictions=dummy_preds, prediction_probas=None, labels=labels),
            )
            if prediction is not None and prediction.prediction_probas is not None and len(labels) == 2:
                by_reference_dummy = self.correction_for_threshold(
                    by_reference_dummy, threshold, target, labels, prediction.prediction_probas.shape
                )
            if prediction is not None and prediction.prediction_probas is not None:
                # dummy log_loss and roc_auc
                binaraized_target = (target.astype(str).values.reshape(-1, 1) == list(labels)).astype(int)
                dummy_prediction = np.full(
                    prediction.prediction_probas.shape, 1 / prediction.prediction_probas.shape[1]
                )
                if by_reference_dummy is not None:
                    by_reference_dummy.log_loss = log_loss(binaraized_target, dummy_prediction)
                    by_reference_dummy.roc_auc = 0.5

        # model quality
        model_quality: Optional[DatasetClassificationQuality] = None
        if quality_metric is not None:
            model_quality = quality_metric.get_result().current

        return ClassificationDummyMetricResults(
            dummy=current_dummy,
            by_reference_dummy=by_reference_dummy,
            model_quality=model_quality,
            metrics_matrix=metrics_matrix,
        )

    def correction_for_threshold(
        self,
        dummy_results: DatasetClassificationQuality,
        threshold: float,
        target: pd.Series,
        labels: list,
        probas_shape: tuple,
    ):
        if threshold == 1.0:
            coeff_precision = 1.0
        else:
            coeff_precision = min(1.0, 0.5 / (1 - threshold))
        coeff_recall = min(1.0, (1 - threshold) / 0.5)

        tpr: Optional[float] = None
        tnr: Optional[float] = None
        fpr: Optional[float] = None
        fnr: Optional[float] = None
        if (
            dummy_results.tpr is not None
            and dummy_results.tnr is not None
            and dummy_results.fpr is not None
            and dummy_results.fnr is not None
        ):
            tpr = dummy_results.tpr * coeff_recall
            tnr = dummy_results.tnr * coeff_precision
            fpr = dummy_results.fpr * coeff_recall
            fnr = dummy_results.fnr * coeff_precision

        return DatasetClassificationQuality(
            accuracy=dummy_results.accuracy,
            precision=dummy_results.precision * coeff_precision,
            recall=dummy_results.recall * coeff_recall,
            f1=2
            * dummy_results.precision
            * coeff_precision
            * dummy_results.recall
            * coeff_recall
            / (dummy_results.precision * coeff_precision + dummy_results.recall * coeff_recall),
            roc_auc=0.5,
            log_loss=None,
            tpr=tpr,
            tnr=tnr,
            fpr=fpr,
            fnr=fnr,
        )


@default_renderer(wrap_type=ClassificationDummyMetric)
class ClassificationDummyMetricRenderer(MetricRenderer):
    def render_json(self, obj: ClassificationDummyMetric) -> dict:
        result = dataclasses.asdict(obj.get_result())
        return result

    def render_html(self, obj: ClassificationDummyMetric) -> List[BaseWidgetInfo]:
        metric_result = obj.get_result()
        in_table_data = pd.DataFrame(data=["accuracy", "precision", "recall", "f1"])
        columns = ["Metric"]
        if metric_result.by_reference_dummy is not None:
            in_table_data["by_ref"] = [
                metric_result.by_reference_dummy.accuracy,
                metric_result.by_reference_dummy.precision,
                metric_result.by_reference_dummy.recall,
                metric_result.by_reference_dummy.f1,
            ]
            columns.append("Dummy (by rerefence)")

        in_table_data["by_curr"] = [
            metric_result.dummy.accuracy,
            metric_result.dummy.precision,
            metric_result.dummy.recall,
            metric_result.dummy.f1,
        ]
        if "Dummy (by rerefence)" in columns:
            columns.append("Dummy (by current)")
        else:
            columns.append("Dummy")

        if metric_result.model_quality is not None:
            in_table_data["model_quality"] = [
                metric_result.model_quality.accuracy,
                metric_result.model_quality.precision,
                metric_result.model_quality.recall,
                metric_result.model_quality.f1,
            ]
            columns.append("Model")

        return [
            header_text(label="Dummy Classification Quality"),
            table_data(column_names=columns, data=np.around(in_table_data, 3).values, title=""),
        ]
