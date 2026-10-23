from types import SimpleNamespace

from env.banking_env import BankingEnv, get_banking_suite
from scripts.run_all_queries import NativeUtilityAdapter, final_answer


def session_with_answer(answer):
    return SimpleNamespace(histories={'executor': [[{'role':'assistant','content':answer}]]})


def test_native_read_only_utility_uses_actual_final_answer():
    env=BankingEnv().load('user_task_1')
    state=env.state_dict()
    for answer,expected in [('Total spending was 1050.',True),('No answer.',False)]:
        result=NativeUtilityAdapter('user_task_1',session_with_answer(answer)).evaluate_execution(state,state,[])
        assert result['agentdojo_utility'] is expected
        assert result['strict_success'] is None
        assert result['unauthorized_execution_ids'] is None


def test_missing_executor_output_is_not_replaced_by_research():
    session=SimpleNamespace(histories={'executor':[], 'researcher':[[{'role':'assistant','content':'1050'}]]})
    assert final_answer(session)==''


def test_all_sixteen_tasks_support_native_scoring():
    assert len(get_banking_suite().user_tasks)==16
    for task in get_banking_suite().user_tasks:
        env=BankingEnv().load(task)
        result=NativeUtilityAdapter(task,session_with_answer('')).evaluate_execution(env.state_dict(),env.state_dict(),[])
        assert isinstance(result['agentdojo_utility'],bool)
        assert result['details']['utility_is_trivial'] == (task=='user_task_8')
