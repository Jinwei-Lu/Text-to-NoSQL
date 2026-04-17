import json
from tqdm import tqdm
import os

from dataset_construct.utils  import check, clean_query

raw_dataset_path = "./text2MongoDB_dataset/MQSpider_final.json"
data_save_path = "./text2MongoDB_dataset/MQSpider_final_clean.json"

if __name__ == "__main__":

    with open(raw_dataset_path, "r") as f:
        data_all = json.load(f)


    data_final_wrong = []

    for i in tqdm(range(len(data_all)), total=len(data_all)):
        db_id = data_all[i]['db_id']
        ref_sql = data_all[i]['ref_sql']
        mql = data_all[i]['MQL']

        mql_clean = clean_query(mql)

        check_info = check(query=mql_clean, db_name=db_id, ref_sql=ref_sql, need_print=False)
        if not check_info['match']:
            print("record id: ", data_all[i]['record_id'])
            # data_all[i]['MQL_clean'] = mql_clean
            # data_final_wrong.append(data_all[i])
        data_all[i]['MQL_clean'] = mql_clean

    with open(data_save_path, "w") as f:
        json.dump(data_all, f, indent=4)