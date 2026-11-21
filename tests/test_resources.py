from dramastill.config.defaults import VideoSettings
from dramastill.media.cooling import CoolingState, Reading, cpu_count, evaluate, reset_for_tests
from dramastill.media.resources import allow_nvenc, chunk_workers, memory_allows, project_slots


def setup_function():
    reset_for_tests(Reading(sensors_ok=False))


def test_balanced_mode_keeps_two_projects_and_cpu_encode():
    settings = VideoSettings()
    settings.cooling_mode = "balanced"
    settings.render_mode = "fast"
    settings.max_parallel_projects = 8
    settings.encoder = "auto"
    assert project_slots(settings) == 2
    assert chunk_workers(settings) == 1
    assert allow_nvenc(settings) is False


def test_low_resource_runs_one_project_on_cpu():
    settings = VideoSettings()
    settings.performance_mode = "maximum"
    settings.render_mode = "low_resource"
    settings.max_parallel_projects = 8
    settings.encoder = "auto"
    assert project_slots(settings) == 1
    assert chunk_workers(settings) == 1
    assert allow_nvenc(settings) is False


def test_cool_mode_without_a_sensor_stays_conservative():
    settings = VideoSettings()
    settings.cooling_mode = "cool"
    settings.render_mode = "fast"
    settings.max_parallel_projects = 8
    settings.encoder = "auto"
    assert project_slots(settings) == 2
    assert chunk_workers(settings) == 1
    assert allow_nvenc(settings) is False


def test_cool_sensor_allows_more_work_and_nvenc():
    reset_for_tests(Reading(cpu_temp_c=46, gpu_temp_c=48, sensors_ok=True, cpu_percent=22, gpu_percent=10))
    settings = VideoSettings()
    settings.cooling_mode = "cool"
    settings.render_mode = "fast"
    settings.max_parallel_projects = 8
    settings.encoder = "auto"
    assert project_slots(settings) == 3
    assert chunk_workers(settings) == min(10, cpu_count())
    assert allow_nvenc(settings) is True
    settings.encoder = "libx264"
    reset_for_tests(Reading(cpu_temp_c=46, sensors_ok=True, cpu_percent=22))
    assert allow_nvenc(settings) is False


def test_balanced_cool_machine_encodes_ten_fragments():
    reset_for_tests(Reading(cpu_temp_c=38, gpu_temp_c=38, sensors_ok=True, cpu_percent=20, gpu_percent=10))
    settings = VideoSettings()
    settings.cooling_mode = "balanced"
    settings.render_mode = "fast"
    settings.encoder = "libx264"
    assert chunk_workers(settings) == min(10, cpu_count())


def test_memory_check_does_not_crash():
    assert memory_allows(VideoSettings()) in {True, False}


def test_heat_steps_up_slowly_and_recovers_slowly():
    settings = VideoSettings()
    settings.cooling_mode = "cool"
    settings.max_parallel_projects = 4
    settings.temp_warning_c = 70
    settings.temp_limit_c = 80
    hot = Reading(cpu_temp_c=90, sensors_ok=True, cpu_percent=40)
    state = CoolingState()
    decision, state = evaluate(settings, hot, state, now=100)
    assert decision.level == 0
    decision, state = evaluate(settings, hot, state, now=121)
    assert decision.level == 1
    assert decision.projects == 1
    assert decision.chunk_workers == 1
    decision, state = evaluate(settings, hot, state, now=130)
    assert decision.level == 1
    decision, state = evaluate(settings, hot, state, now=150)
    assert decision.level == 2
    assert decision.projects == 0
    cool = Reading(cpu_temp_c=50, sensors_ok=True, cpu_percent=10)
    for moment in (171, 192, 213):
        decision, state = evaluate(settings, cool, state, now=moment)
    assert decision.level == 1
    for moment in (234, 255, 276):
        decision, state = evaluate(settings, cool, state, now=moment)
    assert decision.level == 0


def test_user_activity_and_ram_cap_hold_work_back():
    settings = VideoSettings()
    settings.cooling_mode = "cool"
    settings.max_parallel_projects = 6
    active = Reading(cpu_temp_c=40, sensors_ok=True, cpu_percent=15, user_active=True)
    decision, _state = evaluate(settings, active, CoolingState(), now=50)
    assert decision.projects == 1
    heavy = Reading(cpu_temp_c=40, sensors_ok=True, rss_mb=9000)
    settings.max_ram_mb = 4000
    decision, _state = evaluate(settings, heavy, CoolingState(), now=50)
    assert decision.projects == 0


def test_gpu_probe_retries_after_the_cooldown(monkeypatch):
    import dramastill.media.cooling as cooling

    cooling._gpu_missing = True
    cooling._gpu_block_until = 10**12
    calls = []

    def blocked(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("probe should stay cached")

    monkeypatch.setattr(cooling.subprocess, "run", blocked)
    assert cooling._gpu() == (None, None)
    assert calls == []

    class Result:
        returncode = 0
        stdout = "55, 10\n"

    cooling._gpu_block_until = 0
    monkeypatch.setattr(cooling.subprocess, "run", lambda *_args, **_kwargs: Result())
    assert cooling._gpu() == (55.0, 10.0)


def test_pan_stays_inside_the_zoom_margin():
    from dramastill.media.motion import motion_plan

    for preset in ("cinematic", "dramatic", "minimal"):
        settings = VideoSettings()
        settings.preset = preset
        plan = motion_plan(settings)
        assert plan["pan_x"] + plan["shake"] * 0.55 <= plan["margin_x"] + 0.01
        assert plan["pan_y"] + plan["shake"] * 0.4 <= plan["margin_y"] + 0.01
