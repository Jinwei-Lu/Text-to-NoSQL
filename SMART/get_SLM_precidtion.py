'''
负责整理SLM的预测:
query_fields
query_stages
query_filter
target_fields
NoSQL query (text2nosql)
'''

import json
from tqdm import tqdm

if __name__ == "__main__":
    test_data_path = "./data/test/TEND_test_sLM_col_db_alias_target.json"
    prediction_path = "./SLM_prediction/{}/generated_predictions.jsonl"
    save_path = "./data/test/TEND_test_sLM_col_db_alias_target_mql.json"

    with open(test_data_path, "r") as f:
        test_data = json.load(f)


    test_data_new = []
    for hint in ["target_fields", "db_fields", "alias_fields", "query_collection", "text2nosql"]:
    # for hint in ["text2nosql"]:
        hint_data = []
        with open(prediction_path.format(hint), 'r', encoding='utf-8') as file:
            for line in file:
                # 解析每一行的JSON对象并添加到列表中
                example = json.loads(line.strip())
                hint_data.append(example)
        # with open(prediction_path.format(hint), "r", encoding="utf-8") as f:
        #     hint_data = json.load(f)

        print(f"add {hint} hint...")

        for example, example_test in tqdm(zip(hint_data, test_data), total=len(hint_data)):
            example_test[hint + "_pred"] = example['predict']
            test_data_new.append(example_test)

    


    with open(save_path, "w") as f:
        json.dump(test_data_new, f, indent=4)
        

        