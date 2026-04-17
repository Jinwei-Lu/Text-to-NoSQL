# echo "build retrieval codebase..."
# python ./build_vec_lib.py

# echo "retrieve code examples..."
# python ./rag_by_nlq_pref.py 

# # 得到SLM的预测后运行以下指令
# echo "get SLM predictions..."
# python ./get_SLM_prediction.py

topk=${1:-20}
mode=${2:-"normal"}

echo "LLM Debug..."
python ./SMART/LLM_debugger_ori.py --topk $topk --mode $mode

echo "LLM Optimize..."
python ./SMART/LLM_Optimizer_ori.py --topk $topk --mode $mode