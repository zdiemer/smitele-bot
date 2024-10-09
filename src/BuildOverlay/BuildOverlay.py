import asyncio
from tkinter import Tk, ttk, StringVar

from ..SmiteBot.god_builder import GodBuilder
from SmiteProvider import SmiteProvider


class SmiteBuildOverlay:
    __provider: SmiteProvider
    __root: Tk
    __main_frame: ttk.Frame
    __god_builder: GodBuilder

    def __init__(self, provider: SmiteProvider):
        self.__provider = provider
        self.__root = Tk()
        self.__create_window()
        self.__god_builder = GodBuilder()

    async def __get_player_details(self, user_name: str):
        player_ids = await self.__provider.get_player_id_by_name(user_name)

        player_id_field = ttk.Entry(self.__main_frame)
        player_id_field.grid(column=1, row=1)
        player_id_field.delete(0, "end")
        player_id_field.insert(0, str(player_ids[0]["player_id"]))

    def __create_window(self):
        self.__main_frame = ttk.Frame(self.__root, padding=10)
        self.__main_frame.grid()

        ttk.Label(self.__main_frame, text="Username").grid(column=0, row=0)

        input_var = StringVar()
        ttk.Entry(self.__main_frame, textvariable=input_var).grid(
            column=1, row=0, padx=10
        )

        ttk.Button(
            self.__main_frame,
            text="Start",
            command=lambda: asyncio.run(self.__get_player_details(input_var.get())),
        ).grid(column=2, row=0)

        style = ttk.Style(self.__root)
        style.theme_use("clam")

        self.__root.wait_visibility(self.__root)
        self.__root.wm_attributes("-alpha", 0.7, "-topmost", True)

    def start(self):
        self.__root.mainloop()


if __name__ == "__main__":
    provider = SmiteProvider()
    asyncio.run(provider.create())
    SmiteBuildOverlay(provider).start()
