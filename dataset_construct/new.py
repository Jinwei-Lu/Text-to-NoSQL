from dataset_construct.utils  import check
import json
import pandas as pd
from tqdm import tqdm

raw_dataset_path = "./text2MongoDB_dataset/dataset.json"
data_save_path = "./text2MongoDB_dataset/dataset_final.json"

data_ref = pd.read_json("./text2MongoDB_dataset/MQSpider_final_ori.json", orient='records')

with open(data_save_path, "r") as f:
    data_json = json.load(f)
count = {}
data_cor = []
for d in tqdm(data_json, total=len(data_json)):
    if not d['info']['match']:
        continue

    example = {
        "record_id":d['record_id'],
        "db_id": d['db_id'],
        "nl_queries": d['nl_queries'],
        "ref_sql":d['ref_sql'],
        "MQL":""
    }


    check_info = check(d['mql_nodebug'], d['db_id'], d['ref_sql'], need_print=False)

    if check_info['match']:
        example['MQL'] = d['mql_nodebug']
    else:
        check_info = check(d['mql_debugged'], d['db_id'], d['ref_sql'], need_print=False)
        if not check_info['match']:
            continue
        example['MQL'] = d['mql_debugged']


    nlqs = data_ref.loc[data_ref['record_id'] == d['record_id'], 'nl_queries']
    if not nlqs.empty:

        example['nl_queries'] = nlqs.item()
    data_cor.append(example)


# print(count)
with open("./text2MongoDB_dataset/MQSpider_new3.json", "w") as f:
    json.dump(data_cor, f, indent=4)