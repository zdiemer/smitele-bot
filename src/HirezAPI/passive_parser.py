import re
from enum import Enum
from typing import Set

class PassiveAttribute(Enum):
    AURA = 1
    ENEMY_STAT_REDUCTION_AURA = 2
    ENEMY_STRUCTURE_REDUCTION = 3
    BASIC_ATTACK_PROC = 4
    BELOW_THRESHOLD_BUFF = 5
    ALLIED_GODS_BUFF_AURA = 6
    INCREASES_HEALING = 7
    ABILITY_PROC = 8
    ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY = 9
    ANTIHEAL = 10
    FLAT_TRUE_BONUS_DAMAGE = 11
    TRIGGERS_HEAL = 12
    STACKS = 13
    EVOLVES = 14
    INCREASES_WITH_MISSING_STAT = 15
    PERCENT_DAMAGE = 16
    PERCENT_STAT_CONVERTED = 17
    INCREASES_CRITICAL_DAMAGE = 18
    DECREASES_ABILITY_COOLDOWNS = 19
    INCREASES_JUNGLE_MONSTER_DAMAGE = 20
    SHIELD = 21
    ABILITY_HEALING = 22
    IMMUNE_TO_SLOWS = 23
    DECREASES_RELIC_COOLDOWNS = 24
    INCREASES_ENEMY_DAMAGE_WHEN_CC = 25
    EFFECT_VARIES_BY_CURRENT_STATS = 26
    DAMAGE_SCALES_FROM_PROTECTIONS = 27
    DAMAGE_MITIGATION = 28
    BLOCK_STACKS = 29
    DECREASES_CRITICAL_DAMAGE_TAKEN = 30
    ULTIMATE_PROC = 31
    CAUSES_CC = 32
    TRIGGERED_BY_CC = 33
    IMMUNE_TO_CC = 34
    ALLIED_GODS_BUFF_ON_HEAL = 35
    ALLIED_MINIONS_BUFF = 36
    ALLIED_STRUCTURES_BUFF = 37
    ALLOWS_OVERCAPPING_ATTACK_SPEED = 38
    SCALING_BONUS_DAMAGE = 39
    INCREASES_COOLDOWN_CAP = 40
    INCREASED_PROJECTILE_SPEED = 41
    STRIPS_PROTECTIONS = 42
    INCREASE_DAMAGE_BELOW_TARGET_THRESHOLD = 43
    IN_JUNGLE_EFFECT = 44
    DAMAGING_AURA = 45
    EVOLVES_WITH_GOD_KILLS = 46
    EVOLVES_WITH_ASSISTS = 47
    EVOLVES_WITH_MINION_KILLS = 48
    ALLIED_GODS_CAN_CRITICAL_HIT = 49
    CRITICAL_HIT_EFFECT = 50
    AREA_OF_EFFECT_BASIC_ATTACKS = 51
    SELF_BUFF_ON_HEAL = 52
    INCREASES_LIFESTEAL = 53

class PassiveParser:
    ALLIED_GODS_WITHIN = r'(allied gods|allies) within (?P<range>\d+) units'
    ENEMY_GODS_WITHIN = r'(enemy gods|enemies) within (?P<range>\d+) units'
    BASIC_ATTACK = r'(all|your next|your) basic attack(s?)'
    BELOW_BENEATH = r'(?<!targets )(below|beneath)'
    YOUR_ABILITIES = r'(your|your first|casting an|with an) (ability|abilities) (cast|heal|deal)*'
    GAINS_INCREASED_PCT = r'gains (?P<increase>\d+)%'
    ANTIHEAL = r'(their healing reduced)|(reduced healing)'
    EVOLVES = r'(permanent|evolves)'
    PCT_OF_TARGET_HEALTH = r'(\d+)% of the( target|ir current health)'
    SCALES_WITH_PROTS = r'deal an additional (\d+) \+ (\d+%) of your protections'
    PROT_SHRED = r'(decreases enemy|reduce your target\'s) (magical|physical) protection'
    BELOW_PCT_HEALTH = r'targets below (\d+%) health'

    def __init__(self):
        pass

    def parse(self, passive_string: str) -> Set[PassiveAttribute]:
        properties = set()
        passive_string = passive_string.lower().strip()

        # Parse Auras
        if passive_string.startswith('aura'):
            properties.add(PassiveAttribute.AURA)

        # Check if this affects nearby enemies
        if re.search(self.ENEMY_GODS_WITHIN, passive_string) is not None:
            properties.add(PassiveAttribute.ENEMY_STAT_REDUCTION_AURA)

        # Check if this affects nearby structures
        if 'enemy structures' in passive_string:
            properties.add(PassiveAttribute.ENEMY_STRUCTURE_REDUCTION)
            properties.add(PassiveAttribute.ALLIED_STRUCTURES_BUFF)

        # Check if this is proc'd by a basic attack
        if re.search(self.BASIC_ATTACK, passive_string):
            properties.add(PassiveAttribute.BASIC_ATTACK_PROC)

        # Check if this is proc'd when below or beneath a threshold
        if re.search(self.BELOW_BENEATH, passive_string):
            properties.add(PassiveAttribute.BELOW_THRESHOLD_BUFF)

        # Check if this affects nearby gods
        if re.search(self.ALLIED_GODS_WITHIN, passive_string):
            properties.add(PassiveAttribute.ALLIED_GODS_BUFF_AURA)

        # Check if this increases healing done
        if 'healing' in passive_string and 'increased' in passive_string \
                and 'magical lifesteal' not in passive_string:
            properties.add(PassiveAttribute.INCREASES_HEALING)

        # Check if this is proc'd by an ability
        if 'currently on cooldown' not in passive_string \
                and re.search(self.YOUR_ABILITIES, passive_string):
            properties.add(PassiveAttribute.ABILITY_PROC)

        if re.search(self.GAINS_INCREASED_PCT, passive_string):
            properties.add(PassiveAttribute.ALLOWS_OVERCAPPING_PENETRATION_WITH_FIRST_ABILITY)

        if 'over 2.5 attack speed' in passive_string:
            properties.add(PassiveAttribute.ALLOWS_OVERCAPPING_ATTACK_SPEED)

        if re.search(self.ANTIHEAL, passive_string):
            properties.add(PassiveAttribute.ANTIHEAL)

        if 'true damage' in passive_string:
            properties.add(PassiveAttribute.FLAT_TRUE_BONUS_DAMAGE)

        if 'heal yourself' in passive_string:
            properties.add(PassiveAttribute.TRIGGERS_HEAL)

        if 'stacks' in passive_string and \
                'permanent' not in passive_string and \
                'evolves' not in passive_string:
            properties.add(PassiveAttribute.STACKS)

        if re.search(self.EVOLVES, passive_string):
            properties.add(PassiveAttribute.EVOLVES)

        if 'missing' in passive_string:
            properties.add(PassiveAttribute.INCREASES_WITH_MISSING_STAT)

        if re.search(self.PCT_OF_TARGET_HEALTH, passive_string) or \
                'of their current health' in passive_string:
            properties.add(PassiveAttribute.PERCENT_DAMAGE)

        if 'convert' in passive_string:
            properties.add(PassiveAttribute.PERCENT_STAT_CONVERTED)

        if 'critical strike bonus damage' in passive_string:
            properties.add(PassiveAttribute.INCREASES_CRITICAL_DAMAGE)

        if 'cooldown' in passive_string and \
            ('reduce' in passive_string or 'subtract' in passive_string):
            properties.add(PassiveAttribute.DECREASES_ABILITY_COOLDOWNS)

        if 'increased damage to jungle monsters' in passive_string or \
                'jungle monsters and bosses take' in passive_string or \
                'damage against jungle camps' in passive_string:
            properties.add(PassiveAttribute.INCREASES_JUNGLE_MONSTER_DAMAGE)

        if 'shield' in passive_string:
            properties.add(PassiveAttribute.SHIELD)

        if 'abilities heal' in passive_string:
            properties.add(PassiveAttribute.ABILITY_HEALING)

        if 'immune to slows' in passive_string:
            properties.add(PassiveAttribute.IMMUNE_TO_SLOWS)

        if 'relic' in passive_string:
            properties.add(PassiveAttribute.DECREASES_RELIC_COOLDOWNS)

        if 'if they are affected by crowd control' in passive_string:
            properties.add(PassiveAttribute.INCREASES_ENEMY_DAMAGE_WHEN_CC)

        if 'while over' in passive_string and \
                'while under' in passive_string:
            properties.add(PassiveAttribute.EFFECT_VARIES_BY_CURRENT_STATS)

        if re.search(self.SCALES_WITH_PROTS, passive_string):
            properties.add(PassiveAttribute.DAMAGE_SCALES_FROM_PROTECTIONS)

        if 'damage mitigation' in passive_string:
            properties.add(PassiveAttribute.DAMAGE_MITIGATION)

        if 'block stack' in passive_string:
            properties.add(PassiveAttribute.BLOCK_STACKS)

        if 'critical strikes bonus damage taken is decreased' in passive_string:
            properties.add(PassiveAttribute.DECREASES_CRITICAL_DAMAGE_TAKEN)

        if 'when your ultimate ability' in passive_string:
            properties.add(PassiveAttribute.ULTIMATE_PROC)

        if 'stuns' in passive_string or \
                'slows them' in passive_string or \
                'slowed' in passive_string or \
                'slower' in passive_string:
            properties.add(PassiveAttribute.CAUSES_CC)

        if 'with a hard crowd control' in passive_string:
            properties.add(PassiveAttribute.TRIGGERED_BY_CC)

        if 'crowd control immunity' in passive_string or \
                'immune to crowd control' in passive_string:
            properties.add(PassiveAttribute.IMMUNE_TO_CC)

        if 'ability heals' in passive_string or \
            'heal an allied god' in passive_string:
            properties.add(PassiveAttribute.ALLIED_GODS_BUFF_ON_HEAL)

        if 'ally lane minions' in passive_string:
            properties.add(PassiveAttribute.ALLIED_MINIONS_BUFF)

        if 'bonus damage equal to' in passive_string or \
                'bonus damage of' in passive_string or \
                'magical power in damage' in passive_string or \
                'of your physical power' in passive_string:
            properties.add(PassiveAttribute.SCALING_BONUS_DAMAGE)

        if 'cooldown reduction cap' in passive_string:
            properties.add(PassiveAttribute.INCREASES_COOLDOWN_CAP)

        if 'projectile' in passive_string:
            properties.add(PassiveAttribute.INCREASED_PROJECTILE_SPEED)

        if re.search(self.PROT_SHRED, passive_string):
            properties.add(PassiveAttribute.STRIPS_PROTECTIONS)

        if re.search(self.BELOW_PCT_HEALTH, passive_string):
            properties.add(PassiveAttribute.INCREASE_DAMAGE_BELOW_TARGET_THRESHOLD)

        if 'in the jungle' in passive_string:
            properties.add(PassiveAttribute.IN_JUNGLE_EFFECT)

        if 'magical damage per second' in passive_string:
            properties.add(PassiveAttribute.DAMAGING_AURA)

        if 'god kill or assist' in passive_string:
            properties.add(PassiveAttribute.EVOLVES_WITH_GOD_KILLS)

        if 'assists on a minion' in passive_string:
            properties.add(PassiveAttribute.EVOLVES_WITH_ASSISTS)

        if 'killing an enemy god or minion' in passive_string or \
                '1 stack for a minion kill' in passive_string or \
                '1 stack per minion kill' in passive_string or \
                'anything dies' in passive_string:
            properties.add(PassiveAttribute.EVOLVES_WITH_MINION_KILLS)

        if 'your critical' in passive_string or \
                'hitting an enemy god with a critical strike' in passive_string or \
                'critical hits on enemy gods' in passive_string:
            properties.add(PassiveAttribute.CRITICAL_HIT_EFFECT)

        if 'basic attacks will also hit enemies within a 15 unit' in passive_string:
            properties.add(PassiveAttribute.AREA_OF_EFFECT_BASIC_ATTACKS)

        if 'your allies can land a critical strike' in passive_string:
            properties.add(PassiveAttribute.ALLIED_GODS_CAN_CRITICAL_HIT)

        if 'healing yourself' in passive_string:
            properties.add(PassiveAttribute.SELF_BUFF_ON_HEAL)

        if 'lifesteal is increased' in passive_string:
            properties.add(PassiveAttribute.INCREASES_LIFESTEAL)

        return properties