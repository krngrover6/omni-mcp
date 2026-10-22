"""ROS checks never probe real processes, secrets or the network in tests."""
import json
import tempfile
from omniverse_scene_mcp import ros2_check as ros


def test_ros_environment_without_installation_does_not_spawn_commands():
    calls = []
    def refuse(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Unexpected process')
    with tempfile.TemporaryDirectory() as folder:
        report = ros.environment_check(env={}, which=lambda name: None, runner=refuse,
                                       workspace=folder, isaac_root=folder, user_config=folder + '/missing.json')
        assert isinstance(report, dict)
        json.dumps(report, allow_nan=False)
    assert not calls


def test_ros_scrubs_secrets_and_blocks_non_allowlisted_cli_commands():
    secret = 'not-a-real-credential-12345'
    result = ros.scrub({'message': secret}, env={'PRIVATE_TOKEN': secret})
    assert secret not in json.dumps(result)
    called = []
    try:
        ros.run_ros2_cli('ros2', ['topic', 'pub'], runner=lambda *a, **kw: called.append(a))
    except ValueError:
        pass
    else:
        raise AssertionError('Publishing must be refused before spawning a process')
    assert not called
    assert isinstance(ros.setup_plan(), dict)
