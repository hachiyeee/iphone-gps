# 修改手機 GPS - 使用方式

## 連接 USB
* terminal 開啟網頁地圖：
```
cd Desktop/iphone-gps && uv run main.py
```
* 網頁跑在 http://127.0.0.1:9876/ 。
* 手機連線（需輸入密碼🔑）。
```
sudo ~/Desktop/iphone-gps/.venv/bin/python -m pymobiledevice3 remote tunneld            
```
* 等待連線，成功後就可以在網頁上選擇地點囉。

## 用 Wi-Fi 無線連接
* 手機和電腦要連到同一個 Wi-Fi。
* terminal 開啟網頁地圖：
```
cd Desktop/iphone-gps && uv run main.py
```
* 網頁跑在 http://127.0.0.1:9876/ 。
* 手機插著 USB，輸入：
```
uv run pymobiledevice3 lockdown pair          
```
* 手機出現提示，詢問是否信任電腦。按下信任，再執行 remote tunneld（需輸入密碼🔑）。
```
sudo ~/Desktop/iphone-gps/.venv/bin/python -m pymobiledevice3 remote tunneld            
```
* 可以拔掉 USB 了，此時可無線連接，從地圖上選擇地點改 GPS。

*p.s. 網頁切斷後，iPhone 的配對似乎會被忘記，需要重新配對才能無線連接。*