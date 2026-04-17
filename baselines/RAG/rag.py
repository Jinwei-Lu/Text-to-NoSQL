import json
from tqdm import tqdm
from utils.schema_to_markdown import schemas_transform
from utils.utils import generate_reply

SYSTEM_PROMPT = "You are now a natural language interface for a MongoDB database, responsible for converting natural language queries into MongoDB queries."

def prompt_maker(example:dict, if_print:bool):
    instruction = "# Given the MongoDB schema, please convert the following natural language queries into MongoDB queries."
    prompt = instruction + "\n"
    NLQ = example['nlq']
    MQL = example['MQL'].strip("\n")
    db_id = example['db_id']
    rag_examples = example['RAG_examples']

    for example in rag_examples:
        schemas_str = schemas_transform(db_id=example['db_id'])
        nlq = example['NLQ']
        mql = example['MQL'].strip()
        prompt += """

## Natural Language Query: `{}`
## MongoDB Schema
```markdown
{}
```

## MongoDB Query
```javascript
{}
```
""".format(nlq, schemas_str, mql)
        
    prompt += """

## Natural Language Query: `{}`
## MongoDB Schema
```markdown
{}
```

## MongoDB Query
""".format(NLQ, schemas_transform(db_id=db_id), MQL)
    
    if if_print:
        print(prompt)
    return prompt

def generate_rag(example:dict):
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
        ans = ans.rsplit("```javascript", 1)[1].rsplit("```", 1)[0].strip()
    else:
        ans = "db." + ans.rsplit("db.", 1)[1].rsplit(";", 1)[0]
    return ans

if __name__ == "__main__":
    file_name = "./TEND/test_debug_rag20_deepseekv3.json"
    save_path = "./results/results_rag_deepseekv3.json"

    with open(file_name, "r") as f:
        test_data = json.load(f)

    results = []
    for example in tqdm(test_data, total=len(test_data)):

        ans = generate_rag(example=example)
        
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