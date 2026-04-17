import json
from tqdm import tqdm
import time
import os

from debug import debug
from generate import generate
from dataset_construct.utils  import check, remove_ann

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
    for id, example in tqdm(enumerate(data_final), total=len(data_final)):
        record_id = example['record_id']
        db_id = example['db_id']
        nlqs = example['nl_queries']
        ref_sql = example['ref_sql']
        mql_nodebug = example['mql_nodebug']
        mql_debugged = example['mql_debugged']
        info_ = example['info']

        # if info_['match']:
        #     data_final_check.append(example)
        #     continue
        
        example_new = {
            "record_id":record_id,
            "db_id":db_id,
            "nl_queries":nlqs,
            "ref_sql":ref_sql,
            "mql_nodebug":mql_nodebug,
            "mql_debugged":mql_debugged,
            "info":{}
        }
        check_info = check(query=mql_nodebug, db_name=db_id, ref_sql=ref_sql, need_print=False)

        example_new['info'] = check_info
        if check_info['match']:
            mql_debugged = ""
            example_new['info'] = check_info
        elif mql_debugged != "":
            check_info = check(query=mql_debugged, db_name=db_id, ref_sql=ref_sql, need_print=False)
            example_new['info'] = check_info
        data_final_check.append(example_new)
        
    with open(data_save_path, "w") as f:
        json.dump(data_final_check, f, indent=4)

    exit()
    if os.path.exists(data_save_path):
        with open(data_save_path, "r") as f:
            data_final = json.load(f)

    for id, example in tqdm(enumerate(data_all), total=len(data_all)):
        if id < len(data_final):
            continue
        
        print(id)
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

        # Generate

        times = 0
        while True:
            
            try:
                print(f"try generate {times}")
                if times >= 2:
                    model = "gpt-4o-2024-05-13"
                else:
                    model = "gpt-3.5-turbo-16k-0613"
                query = generate(db_name=db_id, nlqs=nlqs, ref_sql=ref_sql, model=model)

                print("*"*50,"\n", query)
                query = remove_ann(query=query)
                times += 1

                
                check_info = check(query=query, db_name=db_id, ref_sql=ref_sql)
                print(check_info)

                if check_info['match'] or times > 2:
                    break

            except Exception as ex:
                print(ex)
                print("wait for 2s...")
                time.sleep(2)

        if check_info['match']:
            example_new['mql_nodebug'] = query
            example_new["info"] = check_info

        else:
            example_new['mql_nodebug'] = query
            times = 0

            # Debug
            while True:
                try:
                    print(f"try debug {times}")
                    if times >= 1:
                        model = "gpt-4o-2024-05-13"
                    else:
                        model = "gpt-3.5-turbo-16k-0613"
                    query_debug = debug(db_name=db_id, nlqs=nlqs, ref_sql=ref_sql, model=model,ori_mql=query)
                    query_debug = remove_ann(query=query_debug)
                    
                    check_info = check(query=query_debug, db_name=db_id, ref_sql=ref_sql)

                    print(check_info)

                    times+=1
                    if check_info['match'] or times > 1:
                        break
                except Exception as ex:
                    print(ex)
                    print("wait for 2s...")
                    time.sleep(2)

            example_new['mql_debugged'] = query_debug
            example_new["info"] = check_info

        data_final.append(example_new)

        with open(data_save_path, "w") as f:
            json.dump(data_final, f, indent=4)