"""
Demonstration of trace-based Agentic AI testing using agentest-ai.

This script tests an agent trace offline and summarizes the findings:
  - Total checks evaluated (165 standardized tests)
  - Passed vs. Failed vs. N/A counts
  - Highlighting specific safety, tool, and trajectory rules
"""

import agentest

def count_verdicts(result):
    counts = {"pass": 0, "fail": 0, "n/a": 0}
    failed_details = []
    for agent_name, checks in result.verdicts.items():
        for check_id, check_data in checks.items():
            status = check_data.get("status", "n/a")
            counts[status] = counts.get(status, 0) + 1
            if status == "fail":
                failed_details.append((check_id, check_data.get("detail", "")))
    return counts, failed_details

def run_hybrid_rag_trace_evaluation():
    print("=======================================================")
    print(" agentest-ai: Offline Deterministic Agent Evaluation  ")
    print("=======================================================")
    
    with agentest.record("hybrid_rag_agent", input_text="What is firmware 4.2.1 policy?") as rec:
        # Step 1: Agent records query rewrite
        rec.step("Rewriting user query with conversation context...")
        
        # Step 2: Agent executes retrieval tool with explicit reason
        rec.tool(
            name="search_documents",
            arguments={"query": "firmware 4.2.1 policy", "top_k": 5},
            result={"chunks": ["Firmware 4.2.1 requires signed admin authorization."]},
            reason="Retrieve document chunks matching user query for firmware policy",
        )
        
        # Step 3: Agent checks grounding and generates answer with citation
        rec.step("Evaluating document relevance and grounding assertions...")
        rec.answer("Firmware 4.2.1 requires signed admin authorization [1].")
        
        # Evaluate trace offline against the 165 Agentic AI rules
        res = rec.check()
        counts, failed = count_verdicts(res)
        
        print("\nTrace Evaluation Results:")
        print(f"  Passed Checks : {counts.get('pass', 0)}")
        print(f"  Failed Checks : {counts.get('fail', 0)}")
        print(f"  N/A Checks    : {counts.get('n/a', 0)}")
        
        if failed:
            print("\nFindings:")
            for check_id, detail in failed[:5]:
                print(f"  [FAIL] {check_id}: {detail}")
        else:
            print("\n[SUCCESS] No trajectory or security violations detected in this run.")

if __name__ == "__main__":
    run_hybrid_rag_trace_evaluation()
    print("\n[Done] Offline test completed with 0 containers, 0 API keys, and 0 network calls.")
