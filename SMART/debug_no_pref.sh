# echo "build retrieval codebase..."
# python ./build_vec_lib.py

# echo "retrieve code examples..."
# python ./rag_by_nlq_pref.py 

# # 得到SLM的预测后运行以下指令
# echo "get SLM predictions..."
# python ./get_SLM_prediction.py

echo "LLM Debug..."
python ./SMART/LLM_debugger_no_pref.py

echo "LLM Optimize..."
python ./SMART/LLM_Optimizer_no_pref.py