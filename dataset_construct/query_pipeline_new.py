import json
from tqdm import tqdm
import time
import os

from generate_feedback import generate_feedback
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

    for id in tqdm(range(len(data_final)), total=len(data_final)):
        example = data_final[id]

        # if id < 4000:
        #     continue

        if example['info']['match'] or example['mql_nodebug'] != "":
            continue
        
        record_id = example['record_id']
        db_id = example['db_id']
        nlqs = example['nl_queries']
        ref_sql = example['ref_sql']

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
        print("*"*50,"\n")
        print("\nrecord id: ", record_id)
        times = 0

        if example['mql_nodebug'] == "":

            while True:
                
                try:
                    
                    model_generate = "gpt-3.5-turbo-16k-0613"

                    print(f"try generate {times} with model {model_generate}")
                    mql = generate(db_name=db_id, nlqs=nlqs, ref_sql=ref_sql, model=model_generate)

                    
                    mql = remove_ann(query=mql)
                    times += 1

                    
                    check_info = check(query=mql, db_name=db_id, ref_sql=ref_sql)
                    print(check_info)

                    if check_info['match'] or times > 0:
                        break

                except Exception as ex:
                    print(ex)
                    print("wait for 2s...")
                    time.sleep(2)
        else:
            mql = example['mql_nodebug']
            check_info = check(query=mql, db_name=db_id, ref_sql=ref_sql)

        example_new['mql_nodebug'] = mql
        example_new["info"] = check_info

        print("MQL:\n", mql)
        print(check_info)

        if not check_info['match']:
            times = 0
            mql_ori = mql
            while True:
                
                try:
                    
                    model_feedback = "gpt-3.5-turbo-16k-0613"
                    model_debug = "gpt-3.5-turbo-0125"

                    print(f"try generate feedback {times} with model {model_feedback}")
                    feedback = generate_feedback(db_name=db_id, nlqs=nlqs, ref_sql=ref_sql, model=model_feedback, mql=mql_ori)

                    print("feedback:\n", feedback)

                    print(f"try debug by feedback {times} with model {model_debug}")
                    try:
                        mql_debugged = debug(nlqs=nlqs, model=model_debug, ori_mql=mql_ori, feedback=feedback)
                    except Exception as ex:
                        mql_debugged = ""
                        print(ex)
                    mql_debugged = remove_ann(query=mql_debugged)
                    times += 1

                    print("mql_debugged:\n", mql_debugged)
                    check_info = check(query=mql_debugged, db_name=db_id, ref_sql=ref_sql)
                    print(check_info)

                    if check_info['match'] or times > 0:
                        break
                    else:
                        mql_ori = mql_debugged

                except Exception as ex:
                    print(ex)
                    print("wait for 2s...")
                    time.sleep(2)

            example_new['mql_debugged'] = mql_debugged
            example_new["info"] = check_info

            print("mql_debugged:\n", mql_debugged)
            print(check_info)

        data_final[id] = example_new

        with open(data_save_path, "w") as f:
            json.dump(data_final, f, indent=4)