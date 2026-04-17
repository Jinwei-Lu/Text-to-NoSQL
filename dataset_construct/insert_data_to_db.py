from pymongo import MongoClient
import os
from tqdm import tqdm
import json

# 连接到MongoDB
client = MongoClient('mongodb://localhost:27017/')


if __name__ == "__main__":

    data_folder_path = "./mongodb_data/"

    for data_file_name in tqdm(os.listdir(data_folder_path), total=len(os.listdir(data_folder_path))):

        data_file_path = os.path.join(data_folder_path, data_file_name)
        with open(data_file_path, "r") as f:
            data_all = json.load(f)

        db_name = data_file_name.split(".", 1)[0]

        client.drop_database(db_name)

        # 选择数据库
        db = client[db_name]

        for collection_name, data_list in data_all.items():
            # 选择集合
            collection = db[collection_name]


            # 插入数据
            try:
                result = collection.insert_many(data_list)
                # print("数据插入成功:", result.inserted_ids)
            except Exception as e:
                print("数据插入失败:", db_name, " -> ", collection_name)
                print("报错:\n", e)

