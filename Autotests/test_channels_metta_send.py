from pathlib import Path


CHANNELS_METTA = Path(__file__).resolve().parents[1] / "src" / "channels.metta"


def test_send_delegates_every_call_to_python_transport():
    """The transport owns deduplication; MeTTa must not swallow a send action.

    Stateful MeTTa-level deduplication can mark a message as sent while an
    alternative evaluation branch never executes the Python transport.  The
    Python Telegram adapter has bounded per-chat deduplication and logging, so
    every evaluated send action must reach it.
    """
    source = CHANNELS_METTA.read_text(encoding="utf-8")
    send_block = source.split("(= (send $msg)", 1)[1].split(";Search the internet", 1)[0]

    assert "telegram.send_message" in send_block
    assert "&lastsend" not in send_block
    assert "change-state!" not in send_block
