# 券商連接器能力與實盤真實度審查報告 (BROKER_CAPABILITY_AUDIT.md)

**審查日期**：2026-10-20  
**審查目標**：全面清查 `anti-gambling-trader-tw` 專案中內建的所有券商連接器（Broker Adapters），評估其實盤可用性、程式碼成熟度與潛在風險。  
**核心宣告**：本審查嚴格恪守安全禁止事項，禁止宣稱未經驗證的範本為實盤可用，禁止任何真實下單行為。

---

## 一、券商連接器整體清單與成熟度評級

經深入檢查 `core/broker/base.py`、`core/broker/paper.py`、`core/broker/registry.py`、`core/broker/registry_tw.py` 及 `core/broker/registry_more.py`，所有券商實作之成熟度與狀態如下表：

| 券商名稱 | 所屬市場 | 類別名稱 | 程式碼狀態 | 成熟度評級 | 能否直接實盤 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **PaperBroker** | 全市場 | `PaperBroker` | 完整實作（記憶體撮合） | **可離線運行** | 否（模擬） |
| **永豐證券 Shioaji** | 台股/台期 | `ShioajiBroker` | 程式碼字串範本（具部分 API 呼叫） | **示意範本 (Stub)** | **絕對不可** |
| **元大證券 SPARK** | 台股/台期 | `YuantaBroker` | 程式碼字串骨架（全為 NotImplementedError） | **純骨架 (Skeleton)** | **絕對不可** |
| **富邦證券新一代** | 台股/海外 | `FubonBroker` | 程式碼字串骨架（全為 NotImplementedError） | **純骨架 (Skeleton)** | **絕對不可** |
| **凱基證券 SUPER PY** | 台股/台期 | `KgiBroker` | 程式碼字串骨架（全為 NotImplementedError） | **純骨架 (Skeleton)** | **絕對不可** |
| **群益/統一/元富** | 台指期貨 | `TwFuturesBroker` | 程式碼字串骨架（全為 NotImplementedError） | **純骨架 (Skeleton)** | **絕對不可** |
| **Interactive Brokers** | 美股/全球 | `IbkrBroker` | 程式碼字串範本（`ib_insync` 示意） | **示意範本 (Stub)** | **絕對不可** |
| **Alpaca** | 美股 | `AlpacaBroker` | 程式碼字串範本（`alpaca-trade-api` 示意） | **示意範本 (Stub)** | **絕對不可** |
| **Tradier** | 美股/選擇權 | `TradierBroker` | 程式碼字串骨架 | **純骨架 (Skeleton)** | **絕對不可** |
| **Binance** | 加密貨幣 | `BinanceBroker` | 程式碼字串範本（`python-binance` 示意） | **示意範本 (Stub)** | **絕對不可** |
| **Pionex** | 加密貨幣 | `PionexBroker` | 程式碼字串範本（唯讀 REST 示意） | **示意範本 (Stub)** | **絕對不可** |
| **OKX / Bybit** | 加密貨幣 | `OkxBroker` / `BybitBroker` | 程式碼字串骨架 | **純骨架 (Skeleton)** | **絕對不可** |
| **CCXT** | 加密貨幣 | `CcxtBroker` | 程式碼字串範本（`ccxt` 示意） | **示意範本 (Stub)** | **絕對不可** |

---

## 二、台股券商連接器深入剖析

### 1. 元大、富邦、凱基與期貨券商骨架 (`registry_tw.py`)

深入閱讀 `core/broker/registry_tw.py` 原始碼發現：
- 這些券商連接器在原專案中**並未被當作 Python 可執行模組載入**，而是封裝在 `BrokerTemplate.code` 字串常數中，僅供 `scaffold` 腳架生成器在產生專案時輸出文字檔。
- 其內部的方法（以 `YuantaBroker` 為例）：
  ```python
  def connect(self) -> None:
      raise NotImplementedError("請依元大 SPARK API 官方文件實作 connect()")
  def get_account(self) -> AccountInfo:
      raise NotImplementedError
  def place_order(self, order: Order) -> OrderResult:
      self._guard_live()
      order.validate()
      raise NotImplementedError
  ```
- **審核結論**：這些連接器只是「佔位符號（Placeholders）」，完全不具備任何交易下單或行情查詢能力。

---

### 2. 永豐證券 Shioaji (`ShioajiBroker`) 深度剖析

`ShioajiBroker` 是原專案中台股範本裡代碼最豐富的一個，但深入稽核發現其距離真正實盤交易存在極大差距與重大風險：

#### (1) 憑證啟用缺失（CA Activation Missing）
在 `connect()` 方法中：
```python
def connect(self) -> None:
    import shioaji as sj
    self.api = sj.Shioaji(simulation=self.simulation)
    self.api.login(api_key=self.api_key, secret_key=self.secret_key)
    # TODO: 依需要載入憑證 self.api.activate_ca(...)(真實下單必要)
```
台股主管機關嚴格規定電子下單必須具備數位憑證簽章。未呼叫 `activate_ca()`，在永豐實盤環境送單將直接被券商端 API 拒絕。

#### (2) 股票「張 vs 股」單位混亂
- 台股集中市場與櫃買市場一般委託係以「張（1,000 股）」為單位，而盤中零股始以「股」為單位。
- `ShioajiBroker` 下單片段：
  ```python
  sj_order = self.api.Order(
      price=order.limit_price or 0,
      quantity=int(order.quantity),
      action=action,
      price_type=price_type,
      order_type=sj.constant.OrderType.ROD,
      account=self.api.stock_account,
  )
  trade = self.api.place_order(contract, sj_order)
  ```
  此處直接將 `order.quantity` 傳入，若呼叫端傳入股數 1,000，將被永豐視為 1,000 張（價值數千萬元至數億元！），引發毀滅性重大交易事故（Fat-finger Error）。

#### (3) 非同步委託與狀態機缺失
- `self.api.place_order()` 回傳的是委託請求物件，非最終成交。
- 原代碼未註冊 Shioaji 的 `order_callback` 或 `status_callback`，無法即時追蹤未成交（Pending）、部分成交（Partially Filled）、已成交（Filled）或已被交易所退單（Failed/Cancelled）的真實狀態。
- 缺少訂單流水號映射與雙向對帳機制。

#### (4) 異常處理與斷線重連機制空白
- 缺少 Session 保持、Heartbeat 監聽、Token 刷新、以及網路斷線自動重連機制。
- 缺少 API 呼叫頻率限制（Rate Limiting）保護。

---

## 三、安全防線機制稽核

`anti-gambling-trader-tw` 專案在安全機制上有一個極佳的硬體級防禦設計：**實盤雙重確認閘門**（`core/broker/base.py`）：

```python
class BrokerAdapter(ABC):
    is_live: bool = False

    def confirm_live_trading(self, *, i_understand_the_risk: bool = False) -> None:
        if i_understand_the_risk is not True:
            self._live_confirmed = False
            raise PermissionError("要開啟真實下單,必須明確傳入 i_understand_the_risk=True。")
        self._live_confirmed = True

    def _guard_live(self) -> None:
        if self.is_live and self._live_confirmed is not True:
            raise PermissionError(
                f"[{self.name}] 真實下單已被安全閘門攔下。\n"
                "請先呼叫 broker.confirm_live_trading(i_understand_the_risk=True)。"
            )
```

### 安全機制評價：
1. **防誤觸保護**：任何將 `is_live` 設為 `True` 的券商連接器，在呼叫 `place_order()` 時第一行都必須執行 `self._guard_live()`。
2. **型別嚴格校驗**：必須傳入字面布林值 `True`（`i_understand_the_risk is not True`），傳入字串 `"true"`、`1` 或 `None` 均會拋出 `PermissionError`。
3. **無硬編碼金鑰**：原始碼中完全沒有寫入任何 API 金鑰、Secret 或憑證密碼，杜絕資安洩漏。

---

## 四、第一階段合規結論與後續執行禁令

依據專案安全規範與審查結果，確立以下禁令與工作準則：

1. **嚴禁實盤下單**：第一階段嚴禁連接任何真實券商帳戶，嚴禁送出真實買賣委託。
2. **定性為示意骨架**：專案內現存之所有台股與海外券商連接器，均定性為「待開發之示意骨架」，不具備實盤交易能力。
3. **全面使用模擬交易**：後續所有 AI 訊號對接、風控驗證、回測與模擬運行，一律使用本地端記憶體模擬的台股 Paper Trading 模組（`TaiwanPaperBroker`）。
4. **未來實盤對接前置要求**（僅供未來規劃參考，本階段不執行）：
   - 必須具備台股「股與張」單位強制檢查器。
   - 必須完成數位憑證硬體安全模組（HSM）或環境變數安全隔離載入。
   - 必須完成雙向非同步回報狀態機（Order State Machine）。
   - 必須通過至少 3 個月以上的離線紙上模擬無故障運行。
