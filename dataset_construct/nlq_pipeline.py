import json
from tqdm import tqdm
import os
from generate_nlq_new import generale_nlq_by_mql

DATA_PATH = "./tend/TEND_no_extend_new.json"
DATA_SAVE_PATH = "./tend/TEND_new.json"

def load_data(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_data(data, file_path):
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def process_example(example, model="gpt-4o-mini"):
    nl_queries = example['nl_queries']
    while len(nl_queries) < 5:
        new_queries = generale_nlq_by_mql(
            nlqs=nl_queries,
            ref_sql=example['ref_sql'],
            db_name=example['db_id'],
            mql=example['MQL'],
            model=model
        )
        nl_queries.extend(new_queries)
    
    return {
        "record_id": example['record_id'],
        "db_id": example['db_id'],
        "nl_queries": nl_queries,
        "ref_sql": example['ref_sql'],
        "MQL": example['MQL']
    }

def main():
    data_all = load_data(DATA_PATH)
    data_final = load_data(DATA_SAVE_PATH) if os.path.exists(DATA_SAVE_PATH) else []

    for id, example in tqdm(enumerate(data_all), total=len(data_all)):
        if id < len(data_final):
            continue
        
        processed_example = process_example(example)
        data_final.append(processed_example)
        save_data(data_final, DATA_SAVE_PATH)

if __name__ == "__main__":
    main()