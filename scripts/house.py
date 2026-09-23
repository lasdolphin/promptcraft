# Маленький деревянный домик.

width = 7        # ширина (x)
depth = 6        # глубина (z)
height = 4       # высота стен

fill(0, -1, 0, width, -1, depth, "cobblestone")               # фундамент
walls(0, 0, 0, width, height, depth, "oak_planks")            # стены
fill(1, 0, 1, width - 1, height, depth - 1, "air")            # пусто внутри

# Крыша-пирамидка: каждый слой чуть меньше предыдущего
for i in range(4):
    fill(-1 + i, height + 1 + i, -1 + i, width + 1 - i, height + 1 + i, depth + 1 - i, "spruce_planks")

fill(3, 0, 0, 3, 1, 0, "air")        # дверной проём
block(1, 2, 0, "glass_pane")         # окна
block(width - 1, 2, 0, "glass_pane")

say("Домик построен!")
