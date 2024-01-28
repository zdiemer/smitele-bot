from __future__ import annotations
import io
import queue
import random
from typing import Dict, Generator, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageOps

from item import Item, ItemType, ItemTreeNode


class ItemTreeBuilder:
    __items: Dict[int, Item]
    trivia_item: Optional[Item] = None

    def __init__(self, items: Dict[int, Item]):
        self.__items = items

    def __get_direct_children(self, item: Item) -> List[Item]:
        children: List[Item] = []
        for i in self.__items.values():
            if i.parent_item_id == item.id and i.active:
                children.append(i)
        return children

    def __build_item_tree(self, root: ItemTreeNode) -> ItemTreeNode:
        if root.depth == 1 and root.item.root_item_id != root.item.id:
            return self.__build_item_tree(
                ItemTreeNode(self.__items[root.item.root_item_id])
            )
        children = self.__get_direct_children(root.item)
        child_count = 0
        level_width = 0
        child_depth = 0
        for child in children:
            if child.tier == 4 and not child.glyph:
                continue
            child_count += 1
            child_node = self.__build_item_tree(ItemTreeNode(child, root.depth + 1))
            root.add_child(child_node)
            level_width += len(child_node.children)
            child_depth = max(child_depth, child_node.depth)
        root.width = max(root.width, level_width, child_count)
        root.depth = max(root.depth, child_depth)
        return root

    def __level_order(
        self, root: ItemTreeNode
    ) -> Generator[Tuple[ItemTreeNode, int], None, None]:
        nodes: queue.Queue[Tuple[ItemTreeNode, int]] = queue.Queue()
        nodes.put((root, 0))

        while nodes.qsize() > 0:
            node, level = nodes.get()
            yield (node, level)
            for child in node.children:
                nodes.put((child, level + 1))

    async def generate_build_tree(
        self, tree_item: Item, trivia_mode: bool = False
    ) -> io.BytesIO:
        spacing = 24
        thumb_size = 96
        border_width = 2
        if tree_item.type != ItemType.ITEM:
            raise ValueError
        root = self.__build_item_tree(
            ItemTreeNode(self.__items[tree_item.root_item_id])
        )

        item_levels: Dict[int, List[Item]] = {}
        for node, level in self.__level_order(root):
            if level in item_levels:
                item_levels[level].append(node.item)
                continue
            item_levels[level] = [node.item]

        width = (
            (thumb_size * root.width)
            + (spacing * (root.width - 1))
            + (border_width * (root.width + 1))
        )

        height = (
            (thumb_size * root.depth)
            + (spacing * (root.depth - 1))
            + (border_width * (root.depth + 1))
        )

        pos_y = height - thumb_size - 2 * border_width
        image_middles: Dict[int, Tuple[Tuple[int, int], Tuple[int, int]]] = {}
        self.trivia_item = (
            random.choice([item for items in item_levels.values() for item in items])
            if trivia_mode
            else None
        )

        with Image.new("RGBA", (width, height), (250, 250, 250, 0)) as output_image:
            for level, items in sorted(item_levels.items(), key=lambda k: k[0]):
                level_width = (
                    (thumb_size * len(items))
                    + (spacing * (len(items) - 1))
                    + (border_width * (len(items) + 1))
                )

                level_pos_x = 0
                if level_width < width:
                    level_pos_x = int((width / 2) - (level_width / 2))
                level_pos_y = pos_y - level * (thumb_size + spacing + border_width)
                for item in items:
                    if self.trivia_item is not None and item.id == self.trivia_item.id:
                        with Image.new("RGB", (thumb_size, thumb_size)) as image:
                            ImageDraw.Draw(image).text(
                                (thumb_size // 3, thumb_size // 5),
                                "?",
                                font=ImageFont.truetype("arial.ttf", 64),
                            )
                            image = ImageOps.expand(
                                image, border=border_width, fill="white"
                            )
                            output_image.paste(image, (level_pos_x, level_pos_y))
                    else:
                        with await item.get_icon_bytes() as item_bytes:
                            with Image.open(item_bytes) as image:
                                if image.size != (thumb_size, thumb_size):
                                    image = image.resize((thumb_size, thumb_size))
                                if image.mode != "RGBA":
                                    image = image.convert("RGBA")
                                image = ImageOps.expand(
                                    image, border=border_width, fill="white"
                                )
                                output_image.paste(image, (level_pos_x, level_pos_y))
                    middle_x = level_pos_x + int(((thumb_size + 2 * border_width) / 2))
                    image_middles[item.id] = (
                        # Top Middle
                        (middle_x, level_pos_y),
                        # Bottom Middle
                        (middle_x, level_pos_y + thumb_size + 2 * border_width),
                    )
                    level_pos_x = level_pos_x + spacing + thumb_size + border_width
            for node, _ in self.__level_order(root):
                if not any(node.children):
                    continue
                for child in node.children:
                    ImageDraw.Draw(output_image).line(
                        [
                            image_middles[node.item.id][0],
                            image_middles[child.item.id][1],
                        ],
                        fill="white",
                        width=3,
                    )

            file = io.BytesIO()
            output_image.save(file, format="PNG")
            file.seek(0)
            return file
