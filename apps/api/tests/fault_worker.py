"""Subprocess-only crash probe; all paths are supplied by temporary test fixtures."""

import json
import sys
import time
from pathlib import Path

from erp_store import ErpMutations
from erp_store.db import make_engine
from erp_store.models import ProductStatus
from erpilot_api.run_store import RunStore
from sqlalchemy.orm import Session


def mutate(mutations, tool, arguments, token):
    if tool == "create_order":
        return mutations.create_order(
            arguments["customer"], [(i["sku"], i["quantity"]) for i in arguments["items"]],
            note=arguments.get("note"), client_token=token,
        )
    if tool == "set_product_status":
        return mutations.set_product_status(
            arguments["sku"], ProductStatus(arguments["status"]), client_token=token,
        )
    return getattr(mutations, tool)(**arguments, client_token=token)


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    def stop():
        Path(request["marker"]).write_text(request["stage"], encoding="utf-8")
        while True:
            time.sleep(1)  # 父进程在确认故障窗口后强制 kill，不以抛异常模拟崩溃。
    mutations = ErpMutations(make_engine(Path(request["db"])))
    stage = request["stage"]
    if stage == "before_execute":
        stop()
    if stage == "before_commit":
        original = ErpMutations._remember
        def remember(self, *args):
            original(self, *args)
            stop()
        ErpMutations._remember = remember
    if stage == "after_commit":
        original_commit = Session.commit
        def commit(self):
            original_commit(self)
            stop()
        Session.commit = commit
    mutate(mutations, request["tool"], request["arguments"], request["token"])
    if stage == "after_result":
        result = mutations.lookup_token(request["token"]).result
        RunStore(Path(request["runs"])).set_invocation_status(
            request["invocation_id"], "succeeded", {"committed_result": result},
        )
        stop()


if __name__ == "__main__":
    main()
