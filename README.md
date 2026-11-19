# Paper PDF Reader

Windows x64 向けの論文 PDF ビューアの試作です。二段組の本文を選び、翻訳した結果をハイライトのコメントとして PDF に保存できます。

## 使う

- ポータブル版は ZIP をフォルダーごと展開し、PaperPdfReader.exe を実行します。展開したフォルダーはそのまま移動できます。
- インストーラー版は setup.exe を実行します。ユーザー単位でインストールし、管理者権限は不要です。

両方に Python、Qt、MuPDF が入っています。PDF の閲覧と注釈の作成はオフラインで使えます。翻訳には別途 LM Studio または OpenAI 互換の翻訳サービスを設定してください。

Ctrl+O または exe への PDF のドラッグ＆ドロップで開きます。本文を最大3ページまでドラッグで選ぶと、原文を編集、翻訳して注釈にできます。色を押すとハイライトを作り、翻訳を開始します。保存した注釈は標準 PDF のハイライトとコメントとして他のビューアでも読み取れます。

設定とバックアップは exe の横の data に保存します。そこに書き込めない場合は %LOCALAPPDATA%\PaperPdfReader を使います。

## 開発と配布ビルド

Windows x64 用の Python 3.12 で作業フォルダーに .venv を作り、次を実行します。

    .\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
    .\.venv\Scripts\python.exe run_reader.py [PDF]
    .\build.ps1

build.ps1 は PyInstaller のフォルダー形式で配布用の実行ファイル一式を作り、別の場所に ZIP を展開して描画と注釈保存を試験します。その後、同じ実行ファイル一式から Inno Setup 7 でインストーラーを作ります。ISCC.exe の場所が標準の場所と異なる場合は -IsccPath で指定できます。ポータブル版だけを作るときは -PortableOnly を指定してください。成果物は release に出力します。

## ライセンス

Paper PDF Reader は AGPL-3.0-or-later です。依存ソフトウェアのライセンスとソースの入手先は [THIRD_PARTY.md](THIRD_PARTY.md) を参照してください。
