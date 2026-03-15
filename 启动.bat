@echo off
cd /d "d:\ai客户背调"
echo 正在启动 AI 客户背调器...
echo 启动后请访问 http://localhost:5000
echo 关闭此窗口将停止服务
echo.
python app.py
pause
