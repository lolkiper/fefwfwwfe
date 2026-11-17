from dramastill.config.defaults import VideoSettings
from dramastill.media.resources import allow_nvenc, chunk_workers, cpu_count, memory_allows, project_slots


def test_balanced_mode_keeps_two_projects_and_cpu_encode():
    settings = VideoSettings()
    settings.performance_mode = "balanced"
    settings.render_mode = "fast"
    settings.max_parallel_projects = 8
    settings.encoder = "auto"
    assert project_slots(settings) == 2
    assert chunk_workers(settings) == max(1, min(2, max(1, cpu_count() // 2)))
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


def test_maximum_mode_can_use_nvenc_and_more_projects():
    settings = VideoSettings()
    settings.performance_mode = "maximum"
    settings.render_mode = "fast"
    settings.max_parallel_projects = 8
    settings.encoder = "auto"
    assert project_slots(settings) == 6
    assert chunk_workers(settings) == max(1, min(3, max(1, cpu_count() - 1)))
    assert allow_nvenc(settings) is True
    settings.encoder = "libx264"
    assert allow_nvenc(settings) is False


def test_memory_check_does_not_crash():
    assert memory_allows(VideoSettings()) in {True, False}
