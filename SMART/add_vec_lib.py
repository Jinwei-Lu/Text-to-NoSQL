import openai
import numpy as np
import json
from tqdm import tqdm
import pickle
import os
import demjson

vec_lib = "./vector_store/train_ori.pkl"
save_path = "./vector_store/train.pkl"
train_path = "./TEND/train_SLM_prediction.json"

api_base = "https://openkey.cloud/v1"

# 替换为你的OpenAI API密钥
api_key = "sk-u4QlUZW8nLtaiqyv40338f916f5a4d6c81B42aB83b6fE567"

client = openai.Client(api_key=api_key, base_url=api_base)
cache = {}


with open(vec_lib, 'rb') as f:
    vec_lib = pickle.load(f)



def get_embedding(text, model="text-embedding-ada-002"):
    if text in cache:
        return cache[text]
    embedding = None
    while embedding is None:
        try:
            embedding = client.embeddings.create(input=text, model=model)
        except Exception as ex:
            print(ex)
            
    cache[text] = np.array(embedding.data[0].embedding)
    return np.array(embedding.data[0].embedding)



with open(train_path, "r", encoding="utf-8") as f:
    train_data = json.load(f)

vec_lib_new = []
cols_emb_dict = {}

for example, example_emb in tqdm(zip(train_data, vec_lib), total=len(train_data)):
    query_collection = example['query_collection']
    query_collection_emb = get_embedding(text=query_collection)
    example_emb['query_collection'] = {"value":query_collection, "embedding":query_collection_emb}

    vec_lib_new.append(example_emb)


with open(save_path, "wb") as f:
    pickle.dump(vec_lib_new, f)