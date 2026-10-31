"""普通窗口调查的串行图；协议校验和工具执行复用领域函数。"""

from langgraph.graph import StateGraph, START, END

from .graph_runtime import InvestigationGraphState, SerialGraphRun
from .window_investigation import (
    Drilldown, WindowReport, prepare_window_action, execute_window_action,
)


class WindowGraphRun(SerialGraphRun):
    report_type = WindowReport

    async def validate(self, state):
        self.restore(state)
        self.budget.check()
        prepared = prepare_window_action(self.domain.session, state["raw"], self.domain.strategy, self.budget)
        if isinstance(prepared, WindowReport):
            return self.output(prepared, raw=None, prepared=None)
        return self.output(raw=None, prepared=prepared.model_dump(mode="json"))

    async def tool(self, state):
        self.restore(state)
        action = Drilldown.model_validate(state["prepared"])
        return self.output(execute_window_action(self.domain.session, action, self.domain.strategy,
                                                self.budget), prepared=None)

    def compile(self):
        graph = StateGraph(InvestigationGraphState)
        for name, node in (("decide", self.decide), ("validate", self.validate), ("tool", self.tool)):
            graph.add_node(name, node)
        graph.add_edge(START, "decide")
        for source, target in (("decide", "validate"), ("validate", "tool"), ("tool", "decide")):
            graph.add_conditional_edges(source, lambda state: "end" if state["report"] is not None else "next",
                                        {"end": END, "next": target})
        return graph.compile(checkpointer=self.checkpointer)
