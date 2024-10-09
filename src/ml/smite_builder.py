import json

import pandas as pd
import torch

from torch import nn


class ModelLoader:
    DENSE_FEATURES = ["Account_Level", "Rank_Stat_Conquest", "Mastery_Level"]
    SPARSE_FEATURES = [
        "ItemId1",
        "ItemId2",
        "ItemId3",
        "ItemId4",
        "ItemId5",
        "ItemId6",
        "ActiveId1",
        "ActiveId2",
    ]

    @staticmethod
    def get_df(file_name):
        with open(
            file_name,
            "r",
            encoding="utf-8",
        ) as f:
            det = list(
                filter(
                    lambda m: m is not None and m["match_queue_id"] == 451,
                    json.loads(f.read()),
                )
            )

            df = pd.DataFrame.from_records(det)
            df = df[
                [
                    "Account_Level",
                    "Rank_Stat_Conquest",
                    "Mastery_Level",
                    "ItemId1",
                    "ItemId2",
                    "ItemId3",
                    "ItemId4",
                    "ItemId5",
                    "ItemId6",
                    "ActiveId1",
                    "ActiveId2",
                    "Win_Status",
                ]
            ]

            df["Win_Status"] = df.apply(
                lambda x: int(x["Win_Status"] == "Winner"), axis=1
            )

            df.fillna(0)

            for c in ModelLoader.SPARSE_FEATURES:
                df[c] = df[c].astype("category").cat.codes

            for c in ModelLoader.DENSE_FEATURES:
                v = df[c].astype("float")
                df[c] = (v - v.mean()) / v.std()

            return df

    @staticmethod
    def train(epochs):
        torch.manual_seed(42)

        df = ModelLoader.get_df(
            "src/match_data_collector/output/match_details_2024-04-17.json"
        )

        dataset = SmiteDataset(df)
        loader = torch.utils.data.DataLoader(dataset, batch_size=10)
        model = RankingModel(df).cuda()
        criterion = torch.nn.BCELoss(reduction="sum")
        optimizer = torch.optim.Adam(model.parameters(), lr=1.0e-3)

        def train(epoch):
            print("Epoch: %d" % epoch)
            model.train()
            train_loss = 0
            for batch_idx, (dense_features, sparse_features, labels) in enumerate(
                loader
            ):
                optimizer.zero_grad()
                outputs = model(dense_features.to("cuda"), sparse_features.to("cuda"))
                loss = criterion(outputs, labels.to("cuda"))
                loss.backward()
                optimizer.step()
                train_loss += loss.item()

            print("Train Loss: %.3f" % (train_loss / (batch_idx + 1)))

        for epoch in range(epochs):
            train(epoch)

        return model


class SmiteDataset(torch.utils.data.Dataset):
    def __init__(self, df):
        self.df = df

    def __len__(self):
        return len(self.df)

    def __getitem__(self, index):
        row = self.df.iloc[index]
        dense_features = [row[feat] for feat in ModelLoader.DENSE_FEATURES]
        sparse_features = [row[feat] for feat in ModelLoader.SPARSE_FEATURES]
        label = [row["Win_Status"]]
        return (
            torch.tensor(dense_features).float(),
            torch.tensor(sparse_features).long(),
            torch.tensor(label).float(),
        )


class RankingModel(nn.Module):
    def __init__(self, df):
        super().__init__()

        embedding_dim = 128

        embeddings = {}
        for c in ModelLoader.SPARSE_FEATURES:
            rows = df[c].max() + 1
            embeddings[c] = nn.Embedding(rows, embedding_dim)

        self.sparse_embeddings = nn.ModuleDict(embeddings)

        self.dense_embedding = nn.Linear(len(ModelLoader.DENSE_FEATURES), embedding_dim)

        self.interaction = nn.Linear(
            (len(ModelLoader.SPARSE_FEATURES) + 1) * embedding_dim, embedding_dim
        )

        self.prediction = nn.Linear(embedding_dim, 1)

    def forward(self, dense_features, sparse_features):
        sparse_embeddings = []
        for c in range(len(ModelLoader.SPARSE_FEATURES)):
            column = ModelLoader.SPARSE_FEATURES[c]
            values = sparse_features[:, c]
            column_embedding = self.sparse_embeddings[column](values)
            sparse_embeddings.append(column_embedding)

        dense_embeddings = self.dense_embedding(dense_features)

        embeddings = torch.cat([dense_embeddings] + sparse_embeddings, dim=1)
        flat_embeddings = torch.nn.functional.relu(embeddings.flatten(start_dim=1))

        interacted = self.interaction(flat_embeddings)
        interacted = torch.nn.functional.relu(interacted)

        prediction = self.prediction(interacted)

        return torch.sigmoid(prediction)


if __name__ == "__main__":
    output_model = ModelLoader.train(1)
    test_df = ModelLoader.get_df(
        "src/match_data_collector/output/match_details_2024-04-16.json"
    )
    test_dataset = SmiteDataset(test_df)
    test_loader = torch.utils.data.DataLoader(test_dataset, batch_size=10)
    correct = 0
    for batch_idx, (dense_features, sparse_features, labels) in enumerate(test_loader):
        outputs = output_model(dense_features.to("cuda"), sparse_features.to("cuda"))
        labels = labels.to("cuda")
        print(outputs.shape, labels.shape)
        correct += torch.eq(outputs, labels).cpu().int().sum()

    print(f"Got {correct} / {len(test_df)} correct")
