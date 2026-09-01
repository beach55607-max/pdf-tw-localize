# PDF 台灣繁中化 (`pdf-tw-localize`)

[English](README.en.md) · [完整 quickstart](examples/quickstart/README.md) · [Skill 規範](SKILL.md)

把版面敏感的英文 PDF 重建成「可追溯、可檢查、保留來源結構」的台灣繁中候選檔。

這不是把 PDF 丟進翻譯器後直接交件的一鍵工具。它把來源頁面、語意區塊、穩定 ID、譯文、重建報告及 QA 證據綁在一起，讓錯字、錯位、漏譯、圖示受損或舊結果混入新版本時能被發現，而不是被漂亮的輸出掩蓋。

## 適合誰

適合：

- 需要保留圖示、線稿、表格、頁碼及原始視覺結構的技術文件。
- 需要知道每段譯文來自哪一頁、哪個來源區塊的團隊。
- 願意把機器檢查、逐頁視覺複核及最後使用者驗收分開的人。
- 想用 Codex Skill 協作，或直接使用可重現 Python 工具鏈的開發者。

不適合：

- 只想取得純文字翻譯、不在意版面或來源追溯。
- 無法合法處理來源 PDF，或想把機密文件直接放進公開 issue。
- 希望任何自動檢查替自己宣告「使用者已接受」。

## 它實際做什麼

```text
英文來源 PDF
  -> 安全預檢與逐頁路由
  -> 語意區塊與穩定 ID
  -> 人工或明確選定的翻譯器產生 zh-TW 譯文
  -> 精確 ID 匯入與驗證
  -> 從英文來源座標重建候選 PDF
  -> 機器 QA -> 語意 QA -> 逐頁視覺複核 -> 使用者驗收
```

本機腳本不會假裝自己呼叫語言模型。主要 LLM、人工譯者或明確選定的翻譯器負責產生譯文；腳本負責來源綁定、穩定 ID、重建及驗證。

## 可檢查的能力

| 能力 | 可直接檢查的實作與測試 |
| --- | --- |
| 穩定 ID 與來源區塊 | [`scripts/extract_segments.py`](scripts/extract_segments.py)、[`tests/test_segment_pipeline.py`](tests/test_segment_pipeline.py) |
| 精確譯文匯入與缺漏／重複拒絕 | [`scripts/import_translations.py`](scripts/import_translations.py)、[`scripts/validate_segments.py`](scripts/validate_segments.py) |
| 可續跑的完整文件批次 | [`scripts/full_run_pipeline.py`](scripts/full_run_pipeline.py)、[`tests/test_full_run_pipeline.py`](tests/test_full_run_pipeline.py) |
| 從來源座標重建與保留未宣告視覺 | [`scripts/rebuild_pdf.py`](scripts/rebuild_pdf.py)、[`tests/test_drawing_signatures.py`](tests/test_drawing_signatures.py) |
| 機器、視覺及使用者狀態分離 | [`references/qa-contract.md`](references/qa-contract.md)、[`scripts/render_review.py`](scripts/render_review.py) |
| 公開套件衛生與合成測試政策 | [`scripts/validate_public_package.py`](scripts/validate_public_package.py)、[`references/synthetic-regression-policy.md`](references/synthetic-regression-policy.md) |

## 10 分鐘安全試跑

以下步驟只做預檢及頁面分析，不會修改來源 PDF，也不會產生可交付候選檔。

參考環境為 CPython 3.11–3.14；精確套件版本及雜湊由 repository 內的 lock files 綁定。

1. 在 Windows PowerShell 建立鎖定環境：

```powershell
.\scripts\setup_env.ps1
.\.venv\Scripts\python.exe .\scripts\verify_runtime.py
```

2. 準備一份你有權處理的英文 PDF。檔名在這裡只用去識別化範例 `sample-guide-en.pdf`；不要把真實客戶文件提交到 repository。

3. 使用全新的輸出目錄執行第一道檢查：

```powershell
$Pdf = Resolve-Path ".\sample-guide-en.pdf"
$RunDir = ".\work\run-001"
New-Item -ItemType Directory -Path $RunDir | Out-Null

& .\.venv\Scripts\python.exe .\scripts\secure_preflight.py $Pdf `
  --output "$RunDir\preflight.json"
if ($LASTEXITCODE -ne 0) { throw "Preflight did not pass; stop and inspect the report." }

& .\.venv\Scripts\python.exe .\scripts\inspect_pdf.py $Pdf `
  --output "$RunDir\inspection.json"
```

看到 `BLOCKED` 就停止；看到 `NEEDS_REVIEW` 就先調查，不要用改檔名或跳過檢查來繼續。完整的抽取、翻譯匯入、重建及 QA 範例見[完整 quickstart](examples/quickstart/README.md)。輸出檔預設拒絕覆寫，重跑時請建立新的 run 目錄。

## 作為 Codex Skill 使用

將這個 repository 放進 Codex 的 `skills/pdf-tw-localize` 目錄，重新開啟 Codex 後，以 `$pdf-tw-localize` 明確呼叫。建議在請求中同時說明：保留原檔、輸出新候選檔、不得繞過 `BLOCKED`，並把 `MACHINE_QA`、`VISUAL_REVIEW` 與 `USER_ACCEPTANCE` 分開回報。

```text
$pdf-tw-localize
把附上的英文 PDF 本地化為台灣繁中；保留原檔並建立新候選檔。
若安全或來源檢查 BLOCKED 就停止；逐頁完成視覺複核，但不要替我宣告 USER_ACCEPTED。
```

執行細節及強制邊界以 [`SKILL.md`](SKILL.md) 與 [`references/security.md`](references/security.md) 為準。

## 大型文件與續跑

完整文件先做全域術語與跨頁相依分析，再切成不拆語意的雜湊綁定批次。中斷後只會接受仍與目前計畫、來源 manifest、穩定 ID 及批次雜湊一致的結果；翻譯 checkpoint 不能繼承舊候選的 QA 或驗收狀態。

指令與結果格式見 [`references/full-mode-acceleration.md`](references/full-mode-acceleration.md)。

## 完成不等於驗收

- `MACHINE_QA`：自動檢查目前宣告範圍的內容、幾何、字型、圖片與向量證據。
- `SEMANTIC_QA`：確認受保護值、條件、角色及跨頁語意沒有被翻錯或混在一起。
- `VISUAL_REVIEW`：真人逐頁開啟比對圖，檢查可讀性、幾何與圖片文字。
- `USER_ACCEPTANCE`：只有使用者明確確認後才能接受；工具不得自行代填。

寫檔成功、PDF 能開啟或 contact sheet 已產生，都不等於上述四項完成。

## 公開與去識別化邊界

公開核心不包含客戶 PDF、真實手冊識別碼、私人詞彙、歷史候選、翻譯紀錄、私人路徑、模型權重、API 金鑰或文件特定證據。公開測試以程式動態生成合成 PDF 物件，不提交真實 PDF fixture。

回報問題時請提供：合成重現步驟、精確指令、狀態 JSON、預期結果及實際結果。請勿上傳客戶文件、含姓名／序號的截圖、憑證、私人目錄、模型檔或翻譯快取。

## 開發與驗證

```powershell
.\scripts\setup_env.ps1 -WithDev
.\.venv\Scripts\python.exe .\scripts\validate_public_package.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
```

若已安裝 `uv`：

```powershell
uv sync --locked --extra dev
uv run python scripts/verify_runtime.py --dev
uv run python scripts/validate_public_package.py
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

`uv.lock` 與 `requirements-*.lock` 記錄精確版本及套件雜湊；第三方套件本體不在 repository 內。

## 授權

本專案依 `AGPL-3.0-only` 發布，完整條文見 [`LICENSE`](LICENSE)。PyMuPDF 可依 AGPL 或商業授權使用；其他依賴與內附 Noto Sans TC 測試字型的聲明見 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。使用者仍應依自己的散布、修改或網路服務情境完成獨立授權審查。
