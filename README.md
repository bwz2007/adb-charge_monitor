<img width="1233" height="924" alt="image" src="https://github.com/user-attachments/assets/2427edcc-d17f-4f5a-96ec-020d6af2657a" />


手机充电功率监测工具（ADB 无线调试 / Wi-Fi）

功能
  1. 通过 Wi-Fi 无线调试连接手机（支持配对码配对、连接、mDNS 自动发现），支持电脑上同时存在多个 adb 设备时选择目标设备
  2. 定时读取电压、电流、电量、温度，实时绘制充电功率曲线
  3. 统计 平均 / 最高 / 最低功率、累计充入能量、有效时长；支持 暂停 / 继续 / 停止；
     可设置采样间隔、总时长、延迟开始、充满自动停止
  4. 独立“保活进程”每 5 秒与手机通信一次，防止息屏后无线调试断开（暂停/停止检测期间依然保活）
  5. 数据导出为 CSV / JSON，曲线保存为 PNG / PDF / SVG

运行环境: Python 3.8+（Windows），依赖: pip install matplotlib
