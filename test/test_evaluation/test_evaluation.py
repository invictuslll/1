import logging.handlers
import os
import shutil
import sys
import time
import unittest
import unittest.mock

import numpy as np

import pynisher

import pytest

from smac.runhistory.runhistory import RunInfo
from smac.stats.stats import Stats
from smac.tae import StatusType
from smac.utils.constants import MAXINT

from autoPyTorch.evaluation.tae import ExecuteTaFuncWithQueue, get_cost_of_crash
from autoPyTorch.pipeline.components.training.metrics.metrics import accuracy, log_loss

this_directory = os.path.dirname(__file__)
sys.path.append(this_directory)
from evaluation_util import get_forecasting_dataset, get_multiclass_classification_datamanager  # noqa E402


def safe_eval_success_mock(*args, **kwargs):
    queue = kwargs['queue']
    queue.put({'status': StatusType.SUCCESS,
               'loss': 0.5,
               'additional_run_info': ''})


class BackendMock(object):
    def __init__(self):
        self.temporary_directory = './.tmp_evaluation'
        try:
            os.mkdir(self.temporary_directory)
        except:  # noqa 3722
            pass

    def load_datamanager(self):
        return get_multiclass_classification_datamanager()


class BackendMockForecasting(object):
    def __init__(self):
        self.temporary_directory = './.tmp_evaluation'
        try:
            os.mkdir(self.temporary_directory)
        except:  # noqa 3722
            pass

    def load_datamanager(self):
        return get_forecasting_dataset()


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        self.datamanager = get_multiclass_classification_datamanager()
        self.tmp = os.path.join(os.getcwd(), '.test_evaluation')
        os.mkdir(self.tmp)
        self.logger_port = logging.handlers.DEFAULT_TCP_LOGGING_PORT
        scenario_mock = unittest.mock.Mock()
        scenario_mock.wallclock_limit = 10
        scenario_mock.algo_runs_timelimit = 1000
        scenario_mock.ta_run_limit = 100
        self.scenario = scenario_mock
        stats = Stats(scenario_mock)
        stats.start_timing()
        self.stats = stats

        try:
            shutil.rmtree(self.tmp)
        except Exception:
            pass

    def tearDown(self):
        try:
            shutil.rmtree(self.tmp)
        except Exception:
            pass

    ############################################################################
    # pynisher tests
    def test_pynisher_memory_error(self):
        def fill_memory():
            a = np.random.random_sample((10000000, 10000000)).astype(np.float64)
            return np.sum(a)

        safe_eval = pynisher.enforce_limits(mem_in_mb=1)(fill_memory)
        safe_eval()
        self.assertEqual(safe_eval.exit_status, pynisher.MemorylimitException)

    def test_pynisher_timeout(self):
        def run_over_time():
            time.sleep(2)

        safe_eval = pynisher.enforce_limits(wall_time_in_s=1,
                                            grace_period_in_s=0)(run_over_time)
        safe_eval()
        self.assertEqual(safe_eval.exit_status, pynisher.TimeoutException)

    ############################################################################
    # Test ExecuteTaFuncWithQueue.run_wrapper()
    @unittest.mock.patch('autoPyTorch.evaluation.tae.eval_train_function')
    def test_eval_with_limits_holdout(self, pynisher_mock):
        pynisher_mock.side_effect = safe_eval_success_mock
        config = unittest.mock.Mock()
        config.config_id = 198
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    multi_objectives=["cost"],
                                    memory_limit=3072,
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        info = ta.run_wrapper(RunInfo(config=config, cutoff=2000000, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[0].config.config_id, 198)
        self.assertEqual(info[1].status, StatusType.SUCCESS, info)
        self.assertEqual(info[1].cost, 0.5)
        self.assertIsInstance(info[1].time, float)

    @unittest.mock.patch('pynisher.enforce_limits')
    def test_cutoff_lower_than_remaining_time(self, pynisher_mock):
        config = unittest.mock.Mock()
        config.config_id = 198
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        self.stats.ta_runs = 1
        ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None, instance_specific=None,
                               seed=1, capped=False))
        self.assertEqual(pynisher_mock.call_args[1]['wall_time_in_s'], 4)
        self.assertIsInstance(pynisher_mock.call_args[1]['wall_time_in_s'], int)

    @unittest.mock.patch('pynisher.enforce_limits')
    def test_eval_with_limits_holdout_fail_timeout(self, pynisher_mock):
        config = unittest.mock.Mock()
        config.config_id = 198

        m1 = unittest.mock.Mock()
        m2 = unittest.mock.Mock()
        m1.return_value = m2
        pynisher_mock.return_value = m1
        m2.exit_status = pynisher.TimeoutException
        m2.wall_clock_time = 30
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.TIMEOUT)
        self.assertEqual(info[1].cost, 1.0)
        self.assertIsInstance(info[1].time, float)
        self.assertNotIn('exitcode', info[1].additional_info)

    @unittest.mock.patch('pynisher.enforce_limits')
    def test_zero_or_negative_cutoff(self, pynisher_mock):
        config = unittest.mock.Mock()
        config.config_id = 198
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        self.scenario.wallclock_limit = 5
        self.stats.submitted_ta_runs += 1
        run_info, run_value = ta.run_wrapper(RunInfo(config=config, cutoff=9, instance=None,
                                             instance_specific=None, seed=1, capped=False))
        self.assertEqual(run_value.status, StatusType.STOP)

    @unittest.mock.patch('autoPyTorch.evaluation.tae.eval_train_function')
    def test_eval_with_limits_holdout_fail_silent(self, pynisher_mock):
        pynisher_mock.return_value = None
        config = unittest.mock.Mock()
        config.origin = 'MOCK'
        config.config_id = 198
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )

        # The following should not fail because abort on first config crashed is false
        info = ta.run_wrapper(RunInfo(config=config, cutoff=60, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.CRASHED)
        self.assertEqual(info[1].cost, 1.0)
        self.assertIsInstance(info[1].time, float)
        self.assertEqual(info[1].additional_info, {'configuration_origin': 'MOCK',
                                                   'error': "Result queue is empty",
                                                   'exit_status': '0',
                                                   'exitcode': 0,
                                                   'subprocess_stdout': '',
                                                   'subprocess_stderr': ''})

        self.stats.submitted_ta_runs += 1
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.CRASHED)
        self.assertEqual(info[1].cost, 1.0)
        self.assertIsInstance(info[1].time, float)
        self.assertEqual(info[1].additional_info, {'configuration_origin': 'MOCK',
                                                   'error': "Result queue is empty",
                                                   'exit_status': '0',
                                                   'exitcode': 0,
                                                   'subprocess_stdout': '',
                                                   'subprocess_stderr': ''})

    @unittest.mock.patch('autoPyTorch.evaluation.tae.eval_train_function')
    def test_eval_with_limits_holdout_fail_memory_error(self, pynisher_mock):
        pynisher_mock.side_effect = MemoryError
        config = unittest.mock.Mock()
        config.config_id = 198
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.MEMOUT)

        # For accuracy, worst possible result is MAXINT
        worst_possible_result = 1
        self.assertEqual(info[1].cost, worst_possible_result)
        self.assertIsInstance(info[1].time, float)
        self.assertNotIn('exitcode', info[1].additional_info)

    @unittest.mock.patch('pynisher.enforce_limits')
    def test_eval_with_limits_holdout_timeout_with_results_in_queue(self, pynisher_mock):
        config = unittest.mock.Mock()
        config.config_id = 198

        def side_effect(**kwargs):
            queue = kwargs['queue']
            queue.put({'status': StatusType.SUCCESS,
                       'loss': 0.5,
                       'additional_run_info': {}})
        m1 = unittest.mock.Mock()
        m2 = unittest.mock.Mock()
        m1.return_value = m2
        pynisher_mock.return_value = m1
        m2.side_effect = side_effect
        m2.exit_status = pynisher.TimeoutException
        m2.wall_clock_time = 30

        # Test for a succesful run
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.SUCCESS)
        self.assertEqual(info[1].cost, 0.5)
        self.assertIsInstance(info[1].time, float)
        self.assertNotIn('exitcode', info[1].additional_info)

        # And a crashed run which is in the queue
        def side_effect(**kwargs):
            queue = kwargs['queue']
            queue.put({'status': StatusType.CRASHED,
                       'loss': 2.0,
                       'additional_run_info': {}})
        m2.side_effect = side_effect
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.CRASHED)
        self.assertEqual(info[1].cost, 1.0)
        self.assertIsInstance(info[1].time, float)
        self.assertNotIn('exitcode', info[1].additional_info)

    @unittest.mock.patch('autoPyTorch.evaluation.tae.eval_train_function')
    def test_eval_with_limits_holdout_2(self, eval_houldout_mock):
        config = unittest.mock.Mock()
        config.config_id = 198

        def side_effect(*args, **kwargs):
            queue = kwargs['queue']
            queue.put({'status': StatusType.SUCCESS,
                       'loss': 0.5,
                       'additional_run_info': kwargs['instance']})
        eval_houldout_mock.side_effect = side_effect
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        self.scenario.wallclock_limit = 180
        instance = "{'subsample': 30}"
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=instance,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.SUCCESS, info)
        self.assertEqual(len(info[1].additional_info), 2)
        self.assertIn('configuration_origin', info[1].additional_info)
        self.assertEqual(info[1].additional_info['message'], "{'subsample': 30}")

    @unittest.mock.patch('autoPyTorch.evaluation.tae.eval_train_function')
    def test_exception_in_target_function(self, eval_holdout_mock):
        config = unittest.mock.Mock()
        config.config_id = 198

        eval_holdout_mock.side_effect = ValueError
        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        self.stats.submitted_ta_runs += 1
        info = ta.run_wrapper(RunInfo(config=config, cutoff=30, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.CRASHED)
        self.assertEqual(info[1].cost, 1.0)
        self.assertIsInstance(info[1].time, float)
        self.assertEqual(info[1].additional_info['error'], 'ValueError()')
        self.assertIn('traceback', info[1].additional_info)
        self.assertNotIn('exitcode', info[1].additional_info)

    def test_silent_exception_in_target_function(self):
        config = unittest.mock.Mock(spec=int)
        config.config_id = 198

        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    )
        ta.pynisher_logger = unittest.mock.Mock()
        self.stats.submitted_ta_runs += 1
        info = ta.run_wrapper(RunInfo(config=config, cutoff=3000, instance=None,
                                      instance_specific=None, seed=1, capped=False))
        self.assertEqual(info[1].status, StatusType.CRASHED, msg=str(info[1].additional_info))
        self.assertEqual(info[1].cost, 1.0)
        self.assertIsInstance(info[1].time, float)
        self.assertIn(
            info[1].additional_info['error'],
            (
                """AttributeError("'BackendMock' object has no attribute """
                """'save_targets_ensemble'",)""",
                """AttributeError("'BackendMock' object has no attribute """
                """'save_targets_ensemble'")""",
                """AttributeError('save_targets_ensemble')"""
                """AttributeError("'BackendMock' object has no attribute """
                """'setup_logger'",)""",
                """AttributeError("'BackendMock' object has no attribute """
                """'setup_logger'")""",
            )
        )
        self.assertNotIn('exitcode', info[1].additional_info)
        self.assertNotIn('exit_status', info[1].additional_info)
        self.assertNotIn('traceback', info[1])

    def test_eval_with_simple_intensification(self):
        config = unittest.mock.Mock(spec=int)
        config.config_id = 198

        ta = ExecuteTaFuncWithQueue(backend=BackendMock(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    budget_type='runtime'
                                    )
        ta.pynisher_logger = unittest.mock.Mock()
        run_info = RunInfo(config=config, cutoff=3000, instance=None,
                           instance_specific=None, seed=1, capped=False)

        for budget in [0.0, 50.0]:
            # Simple intensification always returns budget = 0
            # Other intensifications return a non-zero value
            self.stats.submitted_ta_runs += 1
            run_info = run_info._replace(budget=budget)
            run_info_out, _ = ta.run_wrapper(run_info)
            self.assertEqual(run_info_out.budget, budget)

    def test_eval_forecsating(self):
        config = unittest.mock.Mock(spec=int)
        config.config_id = 198

        ta = ExecuteTaFuncWithQueue(backend=BackendMockForecasting(), seed=1,
                                    stats=self.stats,
                                    memory_limit=3072,
                                    multi_objectives=["cost"],
                                    metric=accuracy,
                                    cost_for_crash=get_cost_of_crash(accuracy),
                                    abort_on_first_run_crash=False,
                                    logger_port=self.logger_port,
                                    pynisher_context='fork',
                                    budget_type='runtime',
                                    )

        ta.pynisher_logger = unittest.mock.Mock()
        run_info = RunInfo(config=config, cutoff=3000, instance=None,
                           instance_specific=None, seed=1, capped=False)

        for budget in [0.0, 50.0]:
            # Simple intensification always returns budget = 0
            # Other intensifications return a non-zero value
            self.stats.submitted_ta_runs += 1
            run_info = run_info._replace(budget=budget)
            run_info_out, _ = ta.run_wrapper(run_info)
            self.assertEqual(run_info_out.budget, budget)


@pytest.mark.parametrize("metric,expected", [(accuracy, 1.0), (log_loss, MAXINT)])
def test_get_cost_of_crash(metric, expected):
    assert get_cost_of_crash(metric) == expected
