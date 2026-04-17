import json
from tqdm import tqdm
from utils.schema_to_markdown import schemas_transform
from utils.utils import generate_reply

SYSTEM_PROMPT = "You are now a MongoDB query optimizer, responsible for checking whether the original MongoDB query is correct based on the natural language query and the database schema. If there are errors, you will analyze and correct them; if there are no errors, you will retain the original MongoDB query."

INSTRUCTION = """# Given a natural language query, a MongoDB Schema, and an original MongoDB query, please follow these steps:
1. Infer the MongoDB fields to be used in the query;
2. Infer the operations to be used in the MongoDB query;
3. Based on the inferences from the previous two steps, determine whether the original MongoDB query is correct. If incorrect, revise it; if correct, retain it;
4. Output according to the following format:
```javascript
db.collection.aggregate([<pipeline>]); / db.collection.find({<filter>}, {<projection>});
```"""

def prompt_maker(example:dict, if_print:bool):
    NLQ = example['nlq']
    db_id = example['db_id']
    MQL_ori = example['text2nosql_pred']
        
    prompt = f"""{INSTRUCTION}

## Natural Language Query: `{NLQ}`
## MongoDB Schema
```markdown
{schemas_transform(db_id=db_id)}
```

## Original MongoDB Query
```javascript
{MQL_ori}
```

A: Let's think step by step!
"""
    
    if if_print:
        print(prompt)
    return prompt

def generate_self_debug(example:dict):
    prompt = prompt_maker(example, False)

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
    ans = generate_reply(messages=messages)[0]
    if "```javascript" in ans:
        ans = ans.rsplit("```javascript", 1)[1].split("```", 1)[0].strip()
    else:
        ans = "db." + ans.rsplit("db.", 1)[1].rsplit(";", 1)[0]
    return ans

if __name__ == "__main__":
    file_name = "./TEND/test_debug_rag20_deepseekv3.json"
    save_path = "./results/results_self_debug_deepseekv3.json"

    with open(file_name, "r") as f:
        test_data = json.load(f)

    results = []
    for example in tqdm(test_data, total=len(test_data)):

        ans = generate_self_debug(example=example)
        
        example_new = {
            "record_id":example['record_id'],
            "db_id":example['db_id'],
            "NLQ":example['nlq'],
            "target":example['MQL'],
            "prediction":ans,
        }

        results.append(example_new)
    
        with open(save_path, "w") as f:
            json.dump(results, f, indent=4)