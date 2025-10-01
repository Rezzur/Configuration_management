def main():
    vfs_name = "VFS"  # имя виртуальной ФС для приглашения
    while True:
        try:
            # приглашение к вводу
            user_input = input(f"{vfs_name}> ").strip()
            if not user_input:
                continue

            # разбор ввода на команду и аргументы
            parts = user_input.split()
            command = parts[0]
            args = parts[1:]

            # обработка команд
            if command == "ls":
                print(f"Команда: ls, Аргументы: {args}")
            elif command == "cd":
                print(f"Команда: cd, Аргументы: {args}")
            elif command == "exit":
                print("Выход из программы.")
                break
            else:
                print(f"Ошибка: неизвестная команда '{command}'")

        except Exception as e:
            print(f"Ошибка выполнения: {e}")


if __name__ == "__main__":
    main()
