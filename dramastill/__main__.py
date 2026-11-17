from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

from dramastill.app import main

# Импорт нужен сборщику EXE: плеер подключается из метода окна.
_FROZEN_MULTIMEDIA = (QAudioOutput, QMediaPlayer)

if __name__ == "__main__":
    main()
