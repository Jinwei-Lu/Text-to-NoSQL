import json
import os
from example_prompt import ask_nlq_mql, ans_nlq_mql
from utils import execute_sql, generate_reply, generate_claude_reply

data_save_path = "./text2MongoDB_dataset/dataset_final.json"
schema_folder_path = "./mongodb_schema/"

def prompt_maker(schemas:dict, nlqs:list, ref_sql:str, db_name:str, mql:str):
    schemas_sql = execute_sql(db_name, ref_sql)['schemas']
    schemas_sql = ", ".join(schemas_sql)
    instruction = """# Predict user question corresponding to MongoDB queries based on MongoDB schemas that differ from the reference user questions, while ensuring that the generated user question have the same meaning as the referenced user questions
NOTE: The user requirements must explicitly mention the necessary MongoDB schemas."""
    schema_prompt = f"""## Schemas of all Collections in "{db_name}" Database\n"""
    nlqs_str = "\n".join(nlqs)

    for collection_name, schema in schemas.items():
        # print()
        schema_prompt += f"""### Collection: {collection_name}
```json
{json.dumps(schema, indent=2)}
```
"""
    prompt = f"""{schema_prompt.strip()}

## MongoDB Query
```javascript
{mql}
```

{instruction}

## The keys required to be displayed in the results: `{schemas_sql}`
## Referenced User Question: 
```plaintext
{nlqs_str}
```

## User Question
A: Let’s think step by step! """
    return prompt



def generale_nlq_by_mql(nlqs:list, ref_sql:str, db_name:str, mql:str, model:str, n=1):

    schema_file_path = os.path.join(schema_folder_path, db_name + ".json")

    with open(schema_file_path, "r") as f:
        schemas = json.load(f)

    prompt = prompt_maker(schemas, nlqs, ref_sql, db_name, mql)

    # print(prompt)
    # return None
    messages = [
        {
            "role":"system",
            "content":"You are an excellent MongoDB query writer, adept at anticipating the user queries corresponding to MongoDB queries."
        },
        {
            "role":"user",
            "content":ask_nlq_mql
        },
        {
            "role":"assistant",
            "content":ans_nlq_mql
        },
        {
            "role":"user",
            "content":prompt
        }
    ]


    if "gpt" in model:
        reply = generate_reply(messages=messages, model=model)
    elif "claude" in model:
        reply = generate_claude_reply(messages=messages, model=model)
    else:
        raise TypeError("Don't support model type {}".format(model))

    return reply


if __name__ == "__main__":
    ref_sql = "SELECT project_details FROM Projects WHERE organisation_id IN ( SELECT organisation_id FROM Projects GROUP BY organisation_id ORDER BY count(*) DESC LIMIT 1 )"
    nlqs = [
        "Can you please provide a list of project details for the projects launched by the organisation?",
        "Please provide a list of project details for the projects launched by the organisation.",
        "List the project details of the projects launched by the organisation"
            
    ]
    db_name = "tracking_grants_for_research"

    mql = "db.Organisation_Types.aggregate([\n  {\n    $unwind: \"$Organisations\"\n  },\n  {\n    $unwind: \"$Organisations.Projects\"\n  },\n  {\n    $group: {\n      _id: \"$Organisations.organisation_id\",\n      project_count: {\n        $sum: 1\n      },\n      projects: {\n        $push: \"$Organisations.Projects\"\n      }\n    }\n  },\n  {\n    $sort: {\n      project_count: -1\n    }\n  },\n  {\n    $limit: 1\n  },\n  {\n    $unwind: \"$projects\"\n  },\n  {\n    $project: {\n      project_details: \"$projects.project_details\",\n      _id: 0\n    }\n  }\n]);\n"


    # prompt = prompt_maker(schemas, nlqs, ref_sql, db_name, mql)

    # print(prompt)

    nlq = generale_nlq_by_mql(nlqs, ref_sql, db_name, mql, "gpt-3.5-turbo-16k-0613")

    if "```" in nlq:
        nlq = nlq.split("```", 2)[1].replace("\n", "")
        

    # print(nlq)