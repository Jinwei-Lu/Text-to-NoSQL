import json
import os

from example_prompt import ask_debug_1, ans_debug_1, ask_debug_2, ans_debug_2
from dataset_construct.utils import execute_sql, generate_reply, get_query_from_reply
from generate_feedback import generate_feedback

schema_folder_path = "./mongodb_schema/"


def prompt_maker(nlqs:list, ori_mql:str, feedback:str):
    instruction = """#### Now, your user has executed the MongoDB query you provided, found that the results do not meet expectations, and provided feedback to you. Please perform the following actions based on the user's feedback:
1 - Identify the error in the MongoDB query statement based on the user's feedback;
2 - Return the corrected MongoDB query statement."""

    nlqs_str = "\n- ".join(nlqs)

    prompt = f"""{instruction}

### Incorrect MongoDB Query
```javascript
{ori_mql}
```
### User Requirement: 
- {nlqs_str}
### User's Feedback
```
{feedback}
```

A: Let’s think step by step!
"""
    return prompt
    
def debug(nlqs:list, model:str, ori_mql:str, feedback:str):

    prompt = prompt_maker(nlqs, ori_mql, feedback)

    if "gpt-4" in model:
        messages = [
            {
                "role":"system",
                "content":"You are an excellent MongoDB query writer, adept at modifying MongoDB query statements based on user feedback and requests."
            },
            {
                "role":"user",
                "content":prompt
            }
        ]
    messages = [
        {
            "role":"system",
            "content":"You are an excellent MongoDB query writer, adept at modifying MongoDB query statements based on user feedback and requests."
        },
        {
            "role":"user",
            "content":ask_debug_1
        },
        {
            "role":"assistant",
            "content":ans_debug_1
        },
        {
            "role":"user",
            "content":ask_debug_2
        },
        {
            "role":"assistant",
            "content":ans_debug_2
        },
        {
            "role":"user",
            "content":prompt
        }
    ]

    # print(prompt)
    # exit()

    reply = generate_reply(messages=messages, model=model)

    # print(reply)

    query = get_query_from_reply(reply)


    return query

if __name__ == "__main__":
    data_save_path = "./text2MongoDB_dataset/dataset_final.json"
    with open(data_save_path, "r") as f:
        data_all = json.load(f)

    id = 103

    data = [ d for d in data_all if d['record_id'] == id][0]

    mql = data['mql_nodebug']
    # pipline = """'[{"$unwind": "$Voting_record"}, {"$match": {"Voting_record.Registration_Date": "08{"$regex": "30"}2015"}}, {"$group": {"_id": "$Voting_record.President_Vote"}}, {"$project": {"_id": 0, "President_Vote": "$_id"}}]'"""
    # print(demjson.decode(pipline))
    # print(mql)
    ref_sql = data['ref_sql']
    db_name = data['db_id']
    nlqs = data['nl_queries']

    feedback = generate_feedback(db_name=db_name, nlqs=nlqs, ref_sql=ref_sql, model="gpt-3.5-turbo-0125", mql=mql)
    # feedback = ans_feedback_2

    # results = excute_mongodb_query(query=query, db_name=db_name)
    # print(results)

    reply = debug(nlqs=nlqs, ori_mql=mql ,model="gpt-3.5-turbo-0125", feedback=feedback)
    print(reply)