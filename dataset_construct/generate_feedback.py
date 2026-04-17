import os
import json
from example_prompt import ask_feedback_1, ask_feedback_2, ans_feedback_1, ans_feedback_2
from dataset_construct.utils  import execute_sql, generate_reply, generate_claude_reply, generate_Yi_reply, excute_mongodb_query

schema_folder_path = "./mongodb_schema/"

def prompt_maker(schemas:dict, nlqs:list, ref_sql:str, db_name:str, mql:str):
    results_sql_dict = execute_sql(db_name, ref_sql)
    schemas_sql = json.dumps(results_sql_dict['schemas'], indent=2)
    results_sql = json.dumps(results_sql_dict['results'], indent=2)

    collection_names = json.dumps(list(schemas.keys()), indent=2)

    try:
        results_mql = excute_mongodb_query(query=mql, db_name=db_name)
        if len(results_mql) > 10:
            results_mql = results_mql[:10]
        if len(results_sql_dict['results']) > 10:
            results_sql_dict['results'] = results_sql_dict['results'][:10]
            results_sql = json.dumps(results_sql_dict['results'], indent=2)

        try:
            results_mql = json.dumps(results_mql, indent=2)
        except:
            results_sql = results_sql_dict['results']
    except Exception as ex:
        results_mql = f"{ex}"

    instruction = """#### Now that you have obtained data from a MongoDB database, please perform the following actions:
1 - Examine the differences between this data and the data you expected to receive;
2 - Analyze where these differences may have originated from with no Solutions."""
    nlqs_str = "\n- ".join(nlqs)
    ann = ""
    if len(nlqs) > 1:
        ann = "(All requirements correspond to the same MongoDB query)"

    prompt = f"""{instruction}

### Data You Expected to receive
```
{results_sql} 
```
### Data You obtained
```
{results_mql}
```

### MongoDB Query
```javascript
{mql}
```

### Collection Names in MongoDB Database
```
{collection_names}
```

### Your Requirement{ann}: 
- {nlqs_str}
### Keys you required to be displayed in the result documents
```
{schemas_sql}
```

A: Let’s think step by step!
"""
    return prompt
    

def generate_feedback(db_name:str, nlqs:list, ref_sql:str, model:str, mql:str):
    schema_file_path = os.path.join(schema_folder_path, db_name + ".json")

    with open(schema_file_path, "r") as f:
        schemas = json.load(f)


    prompt = prompt_maker(schemas, nlqs, ref_sql, db_name, mql)

    messages = [
        {
            "role":"system",
            "content":"You are a user of MongoDB database, and you wish to obtain data from the MongoDB database by making requests. You don't need _id in documents."
        },
        {
            "role":"user",
            "content":ask_feedback_1
        },
        {
            "role":"assistant",
            "content":ans_feedback_1
        },
        {
            "role":"user",
            "content":ask_feedback_2
        },
        {
            "role":"assistant",
            "content":ans_feedback_2
        },
        {
            "role":"user",
            "content":prompt
        }
    ]

    # print(prompt)
    # exit()

    if "gpt" in model:
        reply = generate_reply(messages=messages, model=model)
    elif "claude" in model:
        reply = generate_claude_reply(messages=messages, model=model)
    elif "yi" in model:
        reply = generate_Yi_reply(messages=messages, model=model)
    else:
        raise TypeError("Don't support model type {}".format(model))

    # print(reply)

    return reply

if __name__ == "__main__":

    data_save_path = "./text2MongoDB_dataset/dataset_final.json"
    with open(data_save_path, "r") as f:
        data_all = json.load(f)

    id = 12

    data = [ d for d in data_all if d['record_id'] == id][0]

    mql = data['mql_nodebug']
    ref_sql = data['ref_sql']
    nlqs = data['nl_queries']
    db_id = data['db_id']

    reply = generate_feedback(db_name=db_id, nlqs=nlqs, ref_sql=ref_sql, model="gpt-4o-2024-05-13", mql=mql)
    print(reply)
