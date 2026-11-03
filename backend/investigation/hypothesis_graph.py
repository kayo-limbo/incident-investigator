"""假设调查的LangGraph节点；共享串行调用和恢复运行环境。"""

from langgraph.graph import StateGraph, START, END

from .graph_runtime import InvestigationGraphState, SerialGraphRun
from .hypotheses import HypothesisLedger
from .hypothesis_agent import HYPOTHESIS_ACTION, HypothesisReport, QueryTask
from .hypothesis_steps import PreparedAction, prepare_action, commit_action, execute_query


class GraphState(InvestigationGraphState):
    history: list[dict]
    queries: list[dict]


class HypothesisGraphRun(SerialGraphRun):
    report_type = HypothesisReport
    action_schema = HYPOTHESIS_ACTION
    nodes_per_turn = 4

    def export(self):
        return {**super().export(),
                "history": [h.model_dump(mode="json") for h in self.domain.ledger.history],
                "queries": [q.model_dump(mode="json") for q in self.domain.tasks]}

    def restore(self, state):
        super().restore(state)
        self.domain.ledger = HypothesisLedger.from_history(state["history"])
        self.domain.tasks = [QueryTask.model_validate(q) for q in state["queries"]]

    async def validate(self, state):
        self.restore(state)
        self.budget.check()
        prepared = prepare_action(self.domain, state["raw"], state["turn"], self.budget)
        if isinstance(prepared, HypothesisReport):
            return self.validation_output(prepared, state)
        data = {"action": prepared.action.model_dump(mode="json"),
                "history": [h.model_dump(mode="json") for h in prepared.candidate.history],
                "versions": prepared.versions, "turn": prepared.turn}
        return self.output(raw=None, prepared=data)

    def prepared(self, state):
        item = state["prepared"]
        return PreparedAction(HYPOTHESIS_ACTION.validate_python(item["action"]),
                              HypothesisLedger.from_history(item["history"]), item["versions"], item["turn"])

    async def commit(self, state):
        self.restore(state)
        return self.output(commit_action(self.domain, self.prepared(state), self.budget))

    async def tool(self, state):
        self.restore(state)
        return self.output(execute_query(self.domain, self.prepared(state), self.budget), prepared=None)

    def compile(self):
        graph = StateGraph(GraphState)
        for name, node in (("decide", self.decide), ("validate", self.validate),
                           ("commit", self.commit), ("tool", self.tool)):
            graph.add_node(name, node)
        graph.add_edge(START, "decide")
        for source, target in (("decide", "validate"), ("validate", "commit"),
                               ("commit", "tool"), ("tool", "decide")):
            if source == "validate":
                graph.add_conditional_edges(source, self.validation_route,
                                            {"end": END, "next": target, "correct": "decide"})
            else:
                graph.add_conditional_edges(source, lambda state: "end" if state["report"] is not None else "next",
                                            {"end": END, "next": target})
        return graph.compile(checkpointer=self.checkpointer)
