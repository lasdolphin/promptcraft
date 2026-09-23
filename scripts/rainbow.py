# Радуга из цветной шерсти. Тут есть математика: sin и cos!

colors = ["red_wool", "orange_wool", "yellow_wool", "lime_wool",
          "light_blue_wool", "blue_wool", "purple_wool"]

for i, color in enumerate(colors):
    r = 20 - i                           # каждая полоска чуть меньше
    for angle in range(0, 181, 2):       # полукруг от 0 до 180 градусов
        x = round(r * math.cos(math.radians(angle)))
        y = round(r * math.sin(math.radians(angle)))
        block(x, y, 0, color)
