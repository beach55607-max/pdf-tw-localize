# PDF 台灣繁中化 (`pdf-tw-localize`)

A source-bound Codex Skill and deterministic Python toolchain for rebuilding layout-sensitive English PDFs as monolingual Taiwan Traditional Chinese.

這個專案把 PDF 處理拆成可核對的階段：

```text
英文來源 PDF
  -> 語意區塊與穩定 ID
  -> 主要 LLM 依完整頁面脈絡產生繁中譯文
  -> 精確 ID 匯入與驗證
  -> 從英文來源座標重建候選 PDF
  -> 機器 QA、逐頁視覺複核、使用者驗收
```

本機腳本不會假裝自己呼叫語言模型。主要 LLM、人工譯者或明確選定的外部翻譯器負責產生譯文；腳本負責來源綁定、穩定 ID、重建與驗證。

## 核心能力

- 保留英文來源 PDF，不覆寫原檔。
- 以穩定 ID 綁定來源文字、頁碼、座標、字型、受保護型號／數值與上下文。
- 僅移除明確宣告的文字或向量路徑，保留未宣告圖片與線稿。
- 驗證頁面、字型、顏色、OutputIntent／ICC、英文殘留、幾何、圖片與向量簽章。
- 將 `MACHINE_QA`、`SEMANTIC_QA`、`VISUAL_REVIEW` 與 `USER_ACCEPTANCE` 分開。
- 以明確路徑、ID、版本及 SHA-256 載入可選的資料型 domain pack；不會自動搜尋私人詞彙包。

## 不包含的內容

本公開核心不包含客戶 PDF、私人詞彙、歷史候選、翻譯紀錄、模型權重、API 金鑰或文件特定證據。PyMuPDF、pypdf、Pillow、PyYAML 與選用翻譯後端也不會被複製進 repository；它們由鎖定的環境安裝。

## 需求與安裝

參考環境為 CPython 3.11–3.14。已鎖定並驗證的核心套件為：

- PyMuPDF `1.27.2.2`
- pypdf `6.10.0`
- Pillow `12.1.1`
- PyYAML `6.0.3`（YAML domain pack 與完整測試才需要）

Windows PowerShell：

```powershell
.\scripts\setup_env.ps1
.\.venv\Scripts\python.exe .\scripts\verify_runtime.py
```

要安裝測試依賴並跑完整驗證：

```powershell
.\scripts\setup_env.ps1 -WithDev
.\.venv\Scripts\python.exe .\scripts\validate_public_package.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

若已安裝 `uv`，也可以直接執行：

```powershell
uv sync --locked --extra dev
uv run python scripts/verify_runtime.py --dev
uv run python -m unittest discover -s tests -p "test_*.py"
```

`uv.lock` 與 `requirements-*.lock` 記錄精確版本及套件雜湊；第三方套件本體不在 repository 內。

## 安裝為 Codex Skill

將這個 repository 放入 `$CODEX_HOME/skills/pdf-tw-localize`；若未設定 `CODEX_HOME`，預設位置是使用者目錄下的 `.codex/skills/pdf-tw-localize`。重新開啟 Codex 後，可以用 `$pdf-tw-localize` 明確呼叫，也可由符合描述的 PDF 任務自動選用。

## 使用邊界

1. 先閱讀 [`SKILL.md`](SKILL.md) 與 [`references/security.md`](references/security.md)。
2. 每份輸入先執行 `scripts/secure_preflight.py`；`BLOCKED` 必須停止，`NEEDS_REVIEW` 必須調查。
3. 候選 PDF 必須由英文來源重建，不能拿舊譯本當生成底稿。
4. 機器檢查不能取代逐頁 300 dpi 視覺複核。
5. 只有使用者能把 `USER_ACCEPTANCE` 從 `NOT_CHECKED` 改為接受。

## 測試

公開測試只使用合成文字、座標、圖片及路徑，不含私人文件身分。完整測試涵蓋穩定 ID、domain pack、字型、內容流、向量簽章、來源座標重建、圖片保存及 QA 狀態。

## 授權

本專案依 `AGPL-3.0-only` 發布，完整條文見 [`LICENSE`](LICENSE)。PyMuPDF 可依 AGPL 或商業授權使用；其他依賴與內附 Noto Sans TC 測試字型的聲明見 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。使用者仍應依自己的散布、修改或網路服務情境完成獨立授權審查。
