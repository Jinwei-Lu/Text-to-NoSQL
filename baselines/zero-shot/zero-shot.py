import json
from tqdm import tqdm
from utils.schema_to_markdown import schemas_transform
from utils.utils import generate_reply

SYSTEM_PROMPT = "You are now a natural language interface for a MongoDB database, responsible for converting natural language queries into MongoDB queries."

INSTRUCTION = """# Given a natural language query and a MongoDB Schema, please follow these steps:
1. Infer the MongoDB fields to be used in the query;
2. Infer the operations to be used in the MongoDB query;
3. Generate the MongoDB query based on the inferences from the previous two steps;
4. Output according to the following format:
```javascript
db.collection.aggregate([<pipeline>]); / db.collection.find({<filter>}, {<projection>});
```"""

def prompt_maker(example:dict, if_print:bool):
    # prompt = ICL_PROMPT + "\n"

    NLQ = example['nlq']
    db_id = example['db_id']
        
    prompt = f"""{INSTRUCTION}

## Natural Language Query: `{NLQ}`
## MongoDB Schema
```markdown
{schemas_transform(db_id=db_id)}
```

A: Let's think step by step!
"""
    
    if if_print:
        print(prompt)
    return prompt

def generate_zero_shot(example:dict):
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
    save_path = "./results/results_zero_shot_deepseekv3.json"

    with open(file_name, "r") as f:
        test_data = json.load(f)

    results = []
    for example in tqdm(test_data, total=len(test_data)):

        ans = generate_zero_shot(example=example)
        
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