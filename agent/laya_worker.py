"""Laya decision worker. Runs under a Python that has torch + transformers installed, in its
own process so the MCP server stays light and the model leaves memory when the process exits.

Protocol: one JSON object per line on stdin -> one JSON object per line on stdout.
First line out is {"ready": true, ...} or {"error": "..."}.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL = sys.stdout
sys.stdout = sys.stderr


def send(obj):
    PROTOCOL.write(json.dumps(obj) + "\n")
    PROTOCOL.flush()


def main():
    checkpoint = sys.argv[1] if len(sys.argv) > 1 else "convaiinnovations/laya"
    threads = int(sys.argv[2]) if len(sys.argv) > 2 else 4
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    sys.path.insert(0, HERE)
    try:
        import torch
        torch.set_num_threads(max(1, threads))
        import laya_core as laya
        agent = laya.load(checkpoint, device="cpu")
    except Exception as error:
        send({"error": f"{type(error).__name__}: {error}"})
        return 1
    send({"ready": True, "pid": os.getpid(), "device": str(getattr(agent, "device", "cpu"))})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        if request.get("op") == "quit":
            break
        try:
            result = agent.predict(request["state"], request["questions"])
            answers = {}
            for qid, answer in result["answers"].items():
                answers[qid] = {
                    "choice": answer.get("choice"),
                    "answer_confidence": float(answer.get("answer_confidence") or 0.0),
                    "probabilities": {k: float(v) for k, v in (answer.get("probabilities") or {}).items()},
                }
            send({"id": request.get("id"), "answers": answers})
        except Exception as error:
            send({"id": request.get("id"), "error": f"{type(error).__name__}: {error}"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
