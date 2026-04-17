import json
from tqdm import tqdm
import time
import os

from debug import debug
from generate import generate
from dataset_construct.utils  import check, remove_ann, execute_sql

raw_dataset_path = "./text2MongoDB_dataset/dataset.json"
data_save_path = "./text2MongoDB_dataset/dataset_final.json"

if __name__ == "__main__":

    with open(raw_dataset_path, "r") as f:
        data_all = json.load(f)

    data_final = []
    if os.path.exists(data_save_path):
        with open(data_save_path, "r") as f:
            data_final = json.load(f)

    data_final_check = []

    for id, example in tqdm(enumerate(data_all), total=len(data_all)):
        if id < len(data_final):
            continue

        db_id = example['db_id']
        nlqs = example['question']
        ref_sql = example['query']

        example_new = {
            "record_id":id,
            "db_id":db_id,
            "nl_queries":nlqs,
            "ref_sql":ref_sql,
            "mql_nodebug":"",
            "mql_debugged":"",
            "info":{}
        }

        try:
            execute_sql(db_name=db_id, ref_sql=ref_sql)
        except:
            print(db_id)
            print(ref_sql)