# 第一次使用 Forge8

Forge8 是本機程式碼閱讀桌，不是把整個專案丟給聊天機器人。
先找到相關原碼、加入選段，再提問並對照引用。AI 解釋仍可能慢或出錯。

## 1. 先免模型試用

先依 [README](../README.md#try-it-without-a-model) 複製專案、建立原生 venv，
並以 `python -m pip install .` 安裝此 checkout；不要假設有同名 PyPI 發行版。
在 Forge8 目錄執行 Windows PowerShell 指令：

```powershell
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions --browse-only --state D:\Forge8\browse
```

或使用獨立 WSL venv：

```bash
./.venv-wsl/bin/forge8 read ./examples/isolated-functions --browse-only --state "$HOME/.local/state/forge8-browse"
```

請自行選擇有空間、位於專案之外的絕對 state 路徑；WSL 使用 Linux 檔案系統，
不要放在 `/mnt/c` 或 `/mnt/d`。這裡會保存私人原碼快照。
打開終端機印出的私人網址。此模式只有瀏覽與導航，沒有 AI、試跑或版本比較。

## 2. 先看懂一段程式

1. 打開 `merge_records.py`，點起始行號，再 Shift＋點擊結束行號；也可用「延伸選取」。
2. 選取第 1–18 行，按「加入選段」。手動最多三段，每段最多 80 行。
3. 啟用 AI 後，可問：「重複 key 時保留哪個 value？結果順序如何決定？」
4. 點答案的引用回看原碼。引用有效只代表出處與行號通過檢查，不代表推論正確。

要啟用 AI，先完成 [Windows 安裝指南](INSTALL.md#add-windows-ai-reading)。
腳本會先顯示規劃並要求確認；模型與執行環境放在你選擇的資料夾。
完成後於原終端機按 Ctrl+C，移除 `--browse-only`、`--state` 及其路徑，再啟動：

```powershell
.\.venv\Scripts\forge8.exe read .\examples\isolated-functions
```

開檔不載入模型，明確提問才會。WSL GPU 已在參考機執行過，但全新安裝仍需
進階的 Linux CUDA 建置，尚非一鍵完成；見 [平台限制](PLATFORMS.md)。

## 3. 不知道該讀哪裡時

- 先用「關鍵字找定義」或「同名呼叫位置」；這些不呼叫模型。
- 試試 `exporter/rows.py` 的「此檔案的定義」與「找同名呼叫」，跳到 `export_calls.py`。
- 加入 Python 選段後，可「查看名稱來源」，或明確追蹤匯入來源；不會自動擴充選段。
- 「不知道從哪讀？AI 找原碼（較慢）」是選用功能，可能要數分鐘，也可能漏選原碼。

## 4. 需要時再展開進階功能

「閱讀紀錄、沿用原碼與匯出」可回看答案、沿用同一批原碼或下載含原碼的筆記。
沿用的是來源，不是聊天記憶；新問題仍要寫完整，匯出的筆記也要當私人資料保管。

函式試跑需另外安裝 [WASI 執行環境](isolated-experiments.md)，並逐次明確同意。
它會執行完整模組，不只是畫面選段；可手動釘選一組結果 A，再與下一組輸入 B 比較。
這些是程式自行回報的結果，不是 AI 解釋的驗證。修復與 `observe` 另外會在主機
執行可信任專案的測試，不能當作 WASI 沙箱使用；見 [安全界線](SECURITY.md)。

不用時在終端機按 Ctrl+C；關閉分頁不會停止服務。模型也可用「釋放模型」手動釋放。
[完整用法](USAGE.md) · [實測結果與尚未達成的部分](VALIDATION.md)
