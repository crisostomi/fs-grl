import json
import logging
import math
import operator
from abc import ABC
from collections import Counter
from itertools import groupby
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Union

import hydra
import numpy as np
import omegaconf
import pytorch_lightning as pl
from hydra.utils import instantiate
from omegaconf import DictConfig
from torch.utils.data import DataLoader, Dataset
from torch_geometric.data import Batch, Data

from nn_core.common import PROJECT_ROOT

from fs_grl.data.dataset import EpisodicDataLoader, IterableEpisodicDataset, MapEpisodicDataset, TransferSourceDataset
from fs_grl.data.episode import EpisodeHParams
from fs_grl.data.io_utils import load_data, load_query_support_idxs
from fs_grl.data.utils import flatten

pylogger = logging.getLogger(__name__)


class MetaData:
    def __init__(self, class_to_label_dict, feature_dim, episode_hparams: EpisodeHParams, classes_split: Dict):
        """The data information the Lightning Module will be provided with.
        This is a "bridge" between the Lightning DataModule and the Lightning Module.
        There is no constraint on the class name nor in the stored information, as long as it exposes the
        `save` and `load` methods.
        The Lightning Module will receive an instance of MetaData when instantiated,
        both in the train loop or when restored from a checkpoint.
        This decoupling allows the architecture to be parametric (e.g. in the number of classes) and
        DataModule/Trainer independent (useful in prediction scenarios).
        MetaData should contain all the information needed at test time, derived from its train dataset.
        Examples are the class names in a classification task or the vocabulary in NLP tasks.
        MetaData exposes `save` and `load`. Those are two user-defined methods that specify
        how to serialize and de-serialize the information contained in its attributes.
        This is needed for the checkpointing restore to work properly.
        Args:
            class_to_label_dict
            feature_dim
            episode_hparams
            classes_split
        """
        self.classes_to_label_dict = class_to_label_dict
        self.feature_dim = feature_dim
        self.num_classes = len(class_to_label_dict)
        self.episode_hparams = episode_hparams
        self.classes_split = classes_split

    def save(self, dst_path: Path) -> None:
        """Serialize the MetaData attributes into the zipped checkpoint in dst_path.
        Args:
            dst_path: the root folder of the metadata inside the zipped checkpoint
        """
        pylogger.debug(f"Saving MetaData to '{dst_path}'")

        data = {
            "classes_to_label_dict": self.classes_to_label_dict,
            "feature_dim": self.feature_dim,
            "episode_hparams": self.episode_hparams.as_dict(),
            "classes_split": self.classes_split,
        }

        (dst_path / "data.json").write_text(json.dumps(data, indent=4, default=lambda x: x.__dict__))

    @staticmethod
    def load(src_path: Path) -> "MetaData":
        """Deserialize the MetaData from the information contained inside the zipped checkpoint in src_path.
        Args:
            src_path: the root folder of the metadata inside the zipped checkpoint
        Returns:
            an instance of MetaData containing the information in the checkpoint
        """
        pylogger.debug(f"Loading MetaData from '{src_path}'")

        data = json.loads((src_path / "data.json").read_text(encoding="utf-8"))

        return MetaData(
            class_to_label_dict=data["classes_to_label_dict"],
            feature_dim=data["feature_dim"],
            episode_hparams=EpisodeHParams(**data["episode_hparams"]),
            classes_split=data["classes_split"],
        )


class GraphFewShotDataModule(pl.LightningDataModule, ABC):
    def __init__(
        self,
        dataset_name,
        data_features_to_consider,
        data_dir,
        classes_split_path: Optional[str],
        query_support_split_path,
        separated_query_support: bool,
        support_ratio,
        episode_hparams: EpisodeHParams,
        num_train_episodes,
        num_test_episodes,
        num_workers: DictConfig,
        batch_size: DictConfig,
        gpus: Optional[Union[List[int], str, int]],
        **kwargs,
    ):
        """
        Abstract datamodule for few-shot graph classification.

        :param dataset_name:
        :param data_features_to_consider: whether to consider node tags, degrees or both
        :param data_dir: path to the folder containing the dataset
        :param classes_split_path: path containing the split between base and novel classes
        :param query_support_split_path: path containing the split between queries and supports

        :param separated_query_support: whether to sample queries and supports from disjoint sets
        :param support_ratio: percentage of samples used as support, meaningful only
                            when support and queries are split

        :param episode_hparams: number N of classes per episode, number K of supports per class and
                                number Q of queries per class
        :param num_train_episodes: how many episodes per one training epoch
        :param num_test_episodes: how many episodes for testing

        :param num_workers:
        :param batch_size:
        :param gpus:

        :param kwargs:
        """
        super().__init__()

        self.dataset_name = dataset_name
        self.data_dir = data_dir
        self.classes_split_path = classes_split_path
        self.query_support_split_path = query_support_split_path

        self.episode_hparams = instantiate(episode_hparams)
        self.num_train_episodes = num_train_episodes
        self.num_test_episodes = num_test_episodes

        self.support_ratio = support_ratio
        self.separated_query_support = separated_query_support

        self.num_workers = num_workers
        self.batch_size = batch_size
        self.pin_memory: bool = gpus is not None and str(gpus) != "0"

        self.train_dataset: Optional[Dataset] = None
        self.val_datasets: Optional[Sequence[Dataset]] = None
        self.test_datasets: Optional[Sequence[Dataset]] = None

        self.classes_split = self.get_classes_split()
        self.base_classes, self.novel_classes = self.classes_split["base"], self.classes_split["novel"]

        self.data_list, self.class_to_label_dict = load_data(
            self.data_dir, self.dataset_name, attr_to_consider=data_features_to_consider
        )

        self.labels_split = self.get_labels_split()
        self.base_labels, self.novel_labels = self.labels_split["base"], self.labels_split["novel"]

        self.data_list_by_label = {
            key.item(): list(value) for key, value in groupby(self.data_list, key=operator.attrgetter("y"))
        }

    @property
    def metadata(self) -> MetaData:
        """Data information to be fed to the Lightning Module as parameter.
        Examples are vocabularies, number of classes...
        Returns:
            metadata: everything the model should know about the data, wrapped in a MetaData object.
        """
        # Since MetaData depends on the training data, we need to ensure the setup method has been called.
        if self.train_dataset is None:
            self.setup(stage="fit")

        metadata = MetaData(
            class_to_label_dict=self.class_to_label_dict,
            feature_dim=self.feature_dim,
            episode_hparams=self.episode_hparams,
            classes_split=self.classes_split,
        )

        return metadata

    @property
    def feature_dim(self) -> int:
        return self.data_list[0].x.shape[-1]

    def get_labels_split(self) -> Dict:
        """
        Return base and novel labels from the corresponding base and novel classes
        """

        base_labels = sorted([self.class_to_label_dict[stage_cls] for stage_cls in self.base_classes])
        novel_labels = sorted([self.class_to_label_dict[stage_cls] for stage_cls in self.novel_classes])

        labels_split = {"base": base_labels, "novel": novel_labels}

        return labels_split

    def get_classes_split(self) -> Dict:
        """
        Returns the classes split from file if present, else creates and saves a new one.
        """
        if self.classes_split_path is not None:
            classes_split = json.loads(Path(self.classes_split_path).read_text(encoding="utf-8"))
            return classes_split

        pylogger.info("No classes split provided, creating new split.")
        raise NotImplementedError

    def split_query_support(self, data_list: List[Data]) -> Dict[str, Sequence]:
        """
        Returns the indices splitting query and support samples.
        These are obtained from a split file if it exists, otherwise they are created on the fly.
        :param data_list:
        :return:
        """
        if self.query_support_split_path is not None:
            query_idxs, support_idxs = load_query_support_idxs(self.query_support_split_path)
            return {"query_idxs": query_idxs, "support_idxs": support_idxs}

        pylogger.info("No query support split provided, executing the split.")

        idxs = np.arange(len(data_list))
        np.random.shuffle(idxs)

        support_upperbound = math.ceil(self.support_ratio * len(data_list))
        support_idxs = idxs[:support_upperbound]
        query_idxs = idxs[support_upperbound:]

        supports = [data_list[idx] for idx in support_idxs]
        queries = [data_list[idx] for idx in query_idxs]

        return {"supports": supports, "queries": queries}

    def split_base_novel_samples(self) -> Dict[str, List[Data]]:
        """
        Split the samples in base and novel ones according to the labels
        """
        base_samples: List[Data] = [
            samples for key, samples in self.data_list_by_label.items() if key in self.base_labels
        ]
        base_samples = flatten(base_samples)

        novel_samples: List[Data] = [
            samples for key, samples in self.data_list_by_label.items() if key in self.novel_labels
        ]
        novel_samples = flatten(novel_samples)

        return {"base": base_samples, "novel": novel_samples}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(" f"{self.num_workers=}, " f"{self.batch_size=})"


class GraphMetaDataModule(GraphFewShotDataModule):
    def __init__(
        self,
        dataset_name,
        data_features_to_consider,
        data_dir,
        num_workers: DictConfig,
        batch_size: DictConfig,
        gpus: Optional[Union[List[int], str, int]],
        classes_split_path: Optional[str],
        query_support_split_path,
        episode_hparams: EpisodeHParams,
        support_ratio,
        num_train_episodes,
        num_test_episodes,
        separated_query_support,
        **kwargs,
    ):

        super().__init__(
            dataset_name=dataset_name,
            data_features_to_consider=data_features_to_consider,
            data_dir=data_dir,
            classes_split_path=classes_split_path,
            query_support_split_path=query_support_split_path,
            episode_hparams=episode_hparams,
            num_train_episodes=num_train_episodes,
            num_test_episodes=num_test_episodes,
            separated_query_support=separated_query_support,
            support_ratio=support_ratio,
            num_workers=num_workers,
            batch_size=batch_size,
            gpus=gpus,
        )

    def setup(self, stage: Optional[str] = None):

        if stage is None or stage == "fit":

            split_samples = self.split_base_novel_samples()
            base_samples = split_samples["base"]

            if self.separated_query_support:
                base_samples = self.split_query_support(base_samples)

            self.train_dataset = IterableEpisodicDataset(
                samples=base_samples,
                n_episodes=self.num_train_episodes,
                class_to_label_dict=self.class_to_label_dict,
                stage_labels=self.base_labels,
                episode_hparams=self.episode_hparams,
                separated_query_support=self.separated_query_support,
            )

            novel_samples = split_samples["novel"]
            self.test_datasets = [
                MapEpisodicDataset(
                    samples=novel_samples,
                    n_episodes=self.num_train_episodes,
                    stage_labels=self.novel_labels,
                    class_to_label_dict=self.class_to_label_dict,
                    episode_hparams=self.episode_hparams,
                    separated_query_support=False,
                )
            ]

    def train_dataloader(self) -> EpisodicDataLoader:
        return EpisodicDataLoader(
            dataset=self.train_dataset,
            episode_hparams=self.episode_hparams,
            batch_size=self.batch_size.train,
            num_workers=self.num_workers.train,
            pin_memory=self.pin_memory,
        )

    def test_dataloader(self) -> Sequence[EpisodicDataLoader]:
        return [
            EpisodicDataLoader(
                dataset=dataset,
                episode_hparams=self.episode_hparams,
                shuffle=False,
                batch_size=self.batch_size.val,
                num_workers=self.num_workers.val,
                pin_memory=self.pin_memory,
            )
            for dataset in self.test_datasets
        ]

    def val_dataloader(self):
        pass

    def predict_dataloader(self):
        pass


class GraphTransferDataModule(GraphFewShotDataModule):
    def __init__(
        self,
        dataset_name,
        data_features_to_consider,
        data_dir,
        classes_split_path: Optional[str],
        query_support_split_path,
        separated_query_support: bool,
        support_ratio,
        episode_hparams: EpisodeHParams,
        num_train_episodes,
        num_test_episodes,
        train_val_split_ratio,
        num_workers: DictConfig,
        batch_size: DictConfig,
        gpus: Optional[Union[List[int], str, int]],
        **kwargs,
    ):
        super().__init__(
            dataset_name=dataset_name,
            data_features_to_consider=data_features_to_consider,
            data_dir=data_dir,
            classes_split_path=classes_split_path,
            query_support_split_path=query_support_split_path,
            separated_query_support=separated_query_support,
            support_ratio=support_ratio,
            num_train_episodes=num_train_episodes,
            num_test_episodes=num_test_episodes,
            episode_hparams=episode_hparams,
            num_workers=num_workers,
            batch_size=batch_size,
            gpus=gpus,
        )
        self.train_val_split_ratio = train_val_split_ratio

    def setup(self, stage: Optional[str] = None):

        if stage is None or stage == "fit":

            split_samples = self.split_base_novel_samples()
            base_samples, novel_samples = split_samples["base"], split_samples["novel"]

            base_global_to_local_labels = self.convert_to_local_labels(base_samples, "base")
            pylogger.info(f"Base global to local labels: {base_global_to_local_labels}")

            base_train_samples, base_val_samples = self.split_train_val(base_samples)

            self.train_dataset = TransferSourceDataset(
                samples=base_train_samples,
            )

            self.val_datasets = [
                TransferSourceDataset(
                    samples=base_val_samples,
                )
            ]

            novel_global_to_local_labels = self.convert_to_local_labels(novel_samples, "novel")
            pylogger.info(f"Novel global to local labels: {novel_global_to_local_labels}")

            local_novel_labels = [ind for ind, label in enumerate(sorted(self.novel_labels))]

            self.test_datasets = [
                MapEpisodicDataset(
                    samples=novel_samples,
                    n_episodes=self.num_test_episodes,
                    stage_labels=local_novel_labels,
                    class_to_label_dict=self.class_to_label_dict,
                    episode_hparams=self.episode_hparams,
                    separated_query_support=False,
                )
            ]

    def convert_to_local_labels(self, samples, base_or_novel):
        """
        Given a list of samples, reassign their labels to be ordered from 0 to num_labels -1
        e.g. [2, 5, 10] --> [0, 1, 2]
        return the mapping
        :param samples:
        :param base_or_novel:
        :return:
        """
        stage_labels = self.labels_split[base_or_novel]

        global_to_local_labels = {label: ind for ind, label in enumerate(sorted(stage_labels))}

        for sample in samples:
            sample.y.apply_(lambda x: global_to_local_labels[x])

        return global_to_local_labels

    def split_train_val(self, data_list):
        idxs = np.arange(len(data_list))
        np.random.shuffle(idxs)

        train_upperbound = math.ceil(self.train_val_split_ratio * len(data_list))
        train_idxs = idxs[:train_upperbound]
        val_idxs = idxs[train_upperbound:]

        train_samples = [data_list[idx] for idx in train_idxs]
        val_samples = [data_list[idx] for idx in val_idxs]

        print(f"Train label dist: {Counter(sample.y.item() for sample in train_samples)}")
        print(f"Val label dist: {Counter(sample.y.item() for sample in val_samples)}")
        return train_samples, val_samples

    # meta-training training
    def train_dataloader(self) -> DataLoader:

        return DataLoader(
            self.train_dataset,
            batch_size=self.batch_size.train,
            collate_fn=Batch.from_data_list,
            num_workers=self.num_workers.train,
            pin_memory=self.pin_memory,
            shuffle=True,
        )

    # meta-training validation
    def val_dataloader(self) -> Sequence[DataLoader]:
        return [
            DataLoader(
                dataset,
                shuffle=False,
                batch_size=self.batch_size.val,
                num_workers=self.num_workers.val,
                pin_memory=self.pin_memory,
                collate_fn=Batch.from_data_list,
            )
            for dataset in self.val_datasets
        ]

    # meta-testing
    def test_dataloader(self) -> Sequence[EpisodicDataLoader]:
        return [
            EpisodicDataLoader(
                dataset=dataset,
                episode_hparams=self.episode_hparams,
                shuffle=False,
                batch_size=1,
                num_workers=self.num_workers.test,
                pin_memory=self.pin_memory,
            )
            for dataset in self.test_datasets
        ]

    def predict_dataloader(self):
        pass


@hydra.main(config_path=str(PROJECT_ROOT / "conf"), config_name="default")
def main(cfg: omegaconf.DictConfig):
    datamodule: pl.LightningDataModule = hydra.utils.instantiate(cfg.nn.data, _recursive_=False)
    datamodule.setup()
    for x in datamodule.train_dataloader():
        print(x)


if __name__ == "__main__":
    main()
