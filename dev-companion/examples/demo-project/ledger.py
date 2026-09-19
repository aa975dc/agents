"""Dev Companion 使用流程的最小本地示例，不是完整记账应用。"""


def total_expenses(amounts):
    """计算整数金额的合计；拒绝负数与其它输入类型。"""
    total = 0
    for amount in amounts:
        if type(amount) is not int:
            raise TypeError("请输入整数金额")
        if amount < 0:
            raise ValueError("支出金额不能为负数")
        total += amount
    return total


if __name__ == "__main__":
    print("20 元 + 30 元 = {} 元".format(total_expenses([20, 30])))
