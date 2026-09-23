# Каменная башня со стеклянным куполом.
# Попробуй поменять высоту или материалы и запусти снова: run tower

height = 12      # высота башни
radius = 4       # радиус башни
cx = 0           # центр башни: x
cz = radius + 1  # центр башни: z (отодвигаем вперёд, чтобы не строить на себе)

# Пустая внутри круглая башня
cylinder(cx, 0, cz, radius, height, "stone_bricks", hollow=True)

# Зубцы наверху: блок каждые 30 градусов по кругу
for angle in range(0, 360, 30):
    x = cx + round(radius * math.cos(math.radians(angle)))
    z = cz + round(radius * math.sin(math.radians(angle)))
    block(x, height, z, "stone_bricks")

# Стеклянный купол
sphere(cx, height + 1, cz, radius - 1, "glass", hollow=True)

# Дверь (проём из воздуха) — со стороны игрока
fill(cx, 0, cz - radius, cx, 1, cz - radius, "air")

say("Башня готова!")
