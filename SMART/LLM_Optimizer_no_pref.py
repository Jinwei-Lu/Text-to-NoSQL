'''
LLM Optimizer负责根据MQL的执行结果来修改Integrator整合的信息

Execution Fields Match(EFM): 执行得到的文档中的键名是否匹配;
Execution Value Match(EVM): 执行得到的文档中的值是否匹配;
'''

import json
from tqdm import tqdm
import os
import argparse

from utils.utils import generate_reply
from utils.mongosh_exec import MongoShellExecutor
from utils.schema_to_markdown import schemas_transform

executor = MongoShellExecutor()

NEED_PRINT = False

SYSTEM_PROMPT = """You are now the query adjustment component in the MongoDB natural language interface. You are responsible for adjusting the current MongoDB query based on reference examples. If no adjustment is needed, retain the original MongoDB query."""

INSTRUCTION = """# Given MongoDB collections and their fields, a natural language query, the original MongoDB query, the execution result of the original MongoDB query, and reference examples of queries and results, please perform the following actions:
1. Analyze the reference examples of queries and results to summarize the following information:
  - How each stage of the MongoDB query should operate when handling various natural language queries;
  - What key names need to be included in the final documents when handling various natural language queries;
2. Based on the MongoDB collections and their fields and the natural language query, analyze the original MongoDB query and its corresponding execution result (pay special attention to the operations in the query and the key names in the result documents);
3. Combine the analysis of the reference examples from step one and the analysis of the original MongoDB query and its execution result from step two to determine whether the original MongoDB query needs adjustment (such as the operations in the MongoDB query and the key names in the result documents);
  - If adjustments are needed, adjust the original MongoDB query based on the analysis from steps one and two;
  - If no adjustments are needed, retain the original MongoDB query;
4. Output the final MongoDB query in the following format:
```javascript
db.collection.aggregate([<pipeline>]); / db.collection.find({<filter>}, {<projection>});
```"""

def prompt_maker(NLQ, db_id, rag_examples, prediction):
    schemas_str = schemas_transform(db_id=db_id)

    rag_dict = {}
    for rag_example in rag_examples:
        NLQ_rag = rag_example['NLQ']
        db_id_rag = rag_example['db_id']
        
        MQL_rag = rag_example['MQL'].strip("\n").strip()
        try:
            rag_dict[MQL_rag]['NLQ'].append(NLQ_rag)
        except:
            rag_dict[MQL_rag] = {}
            rag_dict[MQL_rag]['NLQ'] = [NLQ_rag]
            rag_dict[MQL_rag]['db_id'] = db_id_rag

    rag_str = ""
    for id, (rag_mql, rag_example) in enumerate(rag_dict.items()):

        try:
            exec_results = executor.execute_query(query=rag_mql, db_name=rag_example['db_id'], get_str=True)
            if isinstance(exec_results, str):
                exec_results = exec_results
            else:
                if len(exec_results) > 10:
                    exec_results = exec_results[:10]
                exec_results = json.dumps(exec_results, indent=2)
        except Exception as ex:
            exec_results = f"Error in Executing Query and Transfroming result into JSON: `{ex}`"
            if "Object of type ObjectId is not JSON serializable" in exec_results:
                exec_results = "Don't show _id in the execution results. Set the _id in project stage to 0, like `{ _id: 0 }`"

        nlqs_str = ""
        for nlq in rag_example['NLQ']:
            nlqs_str += f"   - `{nlq}`\n"
        nlqs_str = nlqs_str.strip("\n").strip()

        rag_str += f"""## {id+1}. Example {id+1}
### Natural Language Query
  {nlqs_str}
### Gold MongoDB Query
```javascript
{rag_mql}
```
### Gold Execution Resutls
```
{exec_results}
```

"""

    rag_str = rag_str.strip("\n").strip()

    exec_results = executor.execute_query(query=prediction, db_name=db_id, get_str=True)
    if isinstance(exec_results, str):
        exec_results = exec_results
    else:
        if len(exec_results) > 10:
            exec_results = exec_results[:10]
        exec_results = json.dumps(exec_results, indent=2)

    prompt = f"""# Reference Exampels:
{rag_str}


# Current Case
##  MongoDB collections and their fields
```markdown
{schemas_str}
```

## Natural Language Query
  - `{NLQ}`
## Original MongoDB Query
```javascript
{prediction}
```
## Execution Results
```
{exec_results}
```

{INSTRUCTION}

A: Let’s think step by step! """

    if NEED_PRINT:
        print(prompt, end="\n" + "*"*100 + "\n")

    return prompt

def optimize_MQL(NLQ, db_id, rag_examples, prediction):
    prompt = prompt_maker(NLQ, db_id, rag_examples, prediction)
    messages = [
        {
            "role":"system",
            "content":SYSTEM_PROMPT
        },
        {
            "role":"user",
            "content":prompt
        }
    ]
    # with open("./prompt.txt", "w") as f:
    #     f.write(prompt)
    # print(prompt)
    # exit()
    reply = None
    while reply is None:
        try:
            # reply = generate_reply(messages=messages, model="gpt-4o-mini-2024-07-18")[0]
            reply = generate_reply(messages=messages)[0]
            if NEED_PRINT:
                print(reply, end= "\n" + "*"*100 + "\n")

            # with open("./prompt.txt", "a") as f:
            #     f.write(reply)
            # exit()

            reply = reply.rsplit("```javascript", 1)[1].split("```", 1)[0].strip("\n").strip()
        except Exception as e:
            print(e)
        # reply = reply.rsplit("```javascript", 1)[1].split("```", 1)[0].strip("\n").strip()
        # exit()
    if NEED_PRINT:
        print(reply, end= "\n" + "*"*100 + "\n")
    # reply = reply.rsplit("```javascript", 1)[1].rsplit("```", 1)[0].strip("\n").strip()
    # exit()


    rows_new = []
    for row in reply.split("\n"):
        if "//" in row:
            row = row.split("//", 1)[0]
        rows_new.append(row)
    reply = "\n".join(rows_new)

    # print(reply)

    return reply

def deal_db_fields(fields_db:str):
    col = fields_db.strip("\n").split("#")
    fields = set([])
    for field_col in col:
        if field_col != "":
            if ":" in field_col:
                fields.update(field_col.split(":")[1].replace(" ", "").replace("\n", "").split(","))
            else:
                fields.update(field_col.replace(" ", "").replace("\n", "").split(","))

    fields = list(fields)
    fields.sort()
    return " , ".join(fields)

if __name__ == "__main__":
    file_path = "./results/no_pref/test_debug_rag20_deepseekv3_ori_no_pref.json"
    save_path = "./results/no_pref/test_debug_rag_exec20_deepseekv3_ori_no_pref.json"


    with open(file_path, "r") as f:
        test_data = json.load(f)

    test_data_new = []
    if os.path.exists(save_path):
        with open(save_path, "r") as f:
            test_data_new = json.load(f)
    

    for index, example in tqdm(enumerate(test_data), total=len(test_data)):
        if index < len(test_data_new):
            continue
        NLQ = example['nlq']
        db_id = example['db_id']
        rag_examples = example['RAG_examples']
        prediction = example['MQL_debug']

        prediction_opt = optimize_MQL(NLQ, db_id, rag_examples, prediction)

        example_new = example.copy()
        example_new['MQL_debug_exec'] = prediction_opt
        del example_new['RAG_examples']

        test_data_new.append(example_new)

        if index % 20 == 0:
            with open(save_path, "w") as f:
                json.dump(test_data_new, f, indent=4)
    with open(save_path, "w") as f:
        json.dump(test_data_new, f, indent=4)
