import time
import random
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="延迟测试",
    description="测试 API 往返延迟和本地计算性能",
    usage="发送「延迟测试」「speedtest」或「延迟」即可",
    type="application"
)

test_cmd = on_command(
    cmd="ping",
    aliases={"speedtest", "延迟"},
    priority=5,
    block=True,
)


@test_cmd.handle()
async def handle_latency_test(bot: Bot, event: Event):
    # ========== 1. API 往返延迟测试 ==========
    t1 = time.perf_counter()
    await bot.send(event, Message("测试消息..."))
    t2 = time.perf_counter()
    api_latency = (t2 - t1) * 1000  # 转换为毫秒

    # ========== 2. 本地计算性能测试 ==========
    t3 = time.perf_counter()

    # 执行 5 次 4位数+4位数 计算取平均值
    calc_results = []
    for _ in range(5):
        a = random.randint(1000, 9999)
        b = random.randint(1000, 9999)
        calc_results.append(a + b)

    t4 = time.perf_counter()
    avg_calc_time = (t4 - t3) / 5 * 1000  # 单次平均用时（毫秒）

    # ========== 3. 发送结果 ==========
    # 取最后一次计算作为展示
    a_last, b_last = a, b
    result_last = calc_results[-1]

    msg = (
        f"信息发送延时：{api_latency:.3f}ms\n\n"
        "数据处理测试\n"
        f"单次平均耗时：{avg_calc_time:.5f}ms\n"
        f"5次总耗时：{avg_calc_time * 5:.3f}ms"
    )

    await test_cmd.finish(Message(msg))
