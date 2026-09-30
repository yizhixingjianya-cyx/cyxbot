"""
杂项工具模块
提供各种通用工具函数
"""
import asyncio
import random


def binomial_sample(n: int, p: float) -> int:
    """
    二项分布采样
    
    Args:
        n: 试验次数
        p: 每次试验成功的概率
    
    Returns:
        成功次数
    """
    if p <= 0:
        return 0
    if p >= 1:
        return n
    
    count = 0
    for _ in range(n):
        if random.random() < p:
            count += 1
    return count


async def to_thread(func, /, *args, **kwargs):
    """
    在单独的线程中异步运行函数
    
    Args:
        func: 要执行的函数
        *args: 传递给函数的位置参数
        **kwargs: 传递给函数的关键字参数
    
    Returns:
        函数的返回值
    """
    return await asyncio.to_thread(func, *args, **kwargs)