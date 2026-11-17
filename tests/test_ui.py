import os

from dramastill.config.defaults import AppSettings
from dramastill.core.secrets import SecretStore
from dramastill.pipeline.preflight import preflight


def test_preflight_requires_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", os.environ["PATH"])
    settings = AppSettings()
    settings.story.automatic = False
    settings.story.idea = ""
    problems = preflight(settings, SecretStore(tmp_path / "secrets"))
    assert any("иде" in item.lower() or "автоматическ" in item.lower() for item in problems)
    assert any("ключ" in item.lower() for item in problems)


def test_window_opens_and_hides_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("DRAMASTILL_DATA", str(tmp_path / "ui"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication([])
    from dramastill.ui.main_window import MainWindow

    window = MainWindow()
    window.resize(1100, 760)
    window.show()
    application.processEvents()
    assert window.stack.count() == 7
    for index in range(window.stack.count()):
        window.stack.setCurrentIndex(index)
        application.processEvents()
    window.llm_key.setText("sk-secretvalue123456")
    window.persist()
    database = (tmp_path / "ui" / "dramastill.sqlite3").read_bytes()
    secret_file = (tmp_path / "ui" / "secrets" / "secrets.bin").read_bytes()
    assert b"sk-secretvalue123456" not in database
    assert b"sk-secretvalue123456" not in secret_file
    window.shutdown()
    window.close()
    application.processEvents()
