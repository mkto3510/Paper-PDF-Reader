"""Fixed error messages that cross module or process boundaries as plain text, in both display languages.

The PDF worker process and the translation service raise errors without knowing the display language,
so each fixed message is written once here as a (Japanese, English) pair and converted when it is shown.
Messages that depend on the run (a server's reply, an operating-system error) are shown as they are.
"""

PAIRS = (
    ("ページのある PDF を指定してください。", "Select a PDF containing pages."),
    ("注釈が見つかりません。", "Annotation not found."),
    ("別のアプリで PDF が変更されました。名前を付けて保存してください。",
     "Another application changed the PDF. Save a copy."),
    ("PDF を開けませんでした。", "Could not open the PDF."),
    ("PDF を開いている間に内容が変わりました。", "PDF changed while it was being opened."),
    ("PDF 処理を終了しました。", "PDF processing has stopped."),
    ("PDF 処理が途中で終了しました。アプリを開き直してください。",
     "PDF processing stopped unexpectedly. Restart the app."),
    ("翻訳サービスの URL が正しくありません。", "Invalid translation service URL."),
    ("サービスからモデル一覧を取得できませんでした。", "The service could not list models."),
    ("訳文が空でした。", "Empty translation."),
    ("文書のプロパティを読み取れませんでした。", "Could not read document properties."),
)

TO_ENGLISH = dict(PAIRS)
TO_JAPANESE = {english: japanese for japanese, english in PAIRS}


def localize(language, message):
    """The message in the display language, or unchanged when it is not a known fixed message."""
    if not isinstance(message, str):
        return message
    table = TO_ENGLISH if language == "en" else TO_JAPANESE
    return table.get(message, message)
