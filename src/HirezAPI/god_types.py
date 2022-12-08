from enum import Enum

class GodId(Enum):
    ACHILLES = 3492
    AGNI = 1737
    AH_MUZEN_CAB = 1956
    AH_PUCH = 2056
    AMATERASU = 2110
    ANHUR = 1773
    ANUBIS = 1668
    AO_KUANG = 2034
    APHRODITE = 1898
    APOLLO = 1899
    ARACHNE = 1699
    ARES = 1782
    ARTEMIS = 1748
    ARTIO = 3336
    ATHENA = 1919
    ATLAS = 4034
    AWILIX = 2037
    BABA_YAGA = 3925
    BACCHUS = 1809
    BAKASURA = 1755
    BARON_SAMEDI = 3518
    BASTET = 1678
    BELLONA = 2047
    CABRAKAN = 2008
    CAMAZOTZ = 2189
    CERBERUS = 3419
    CERNUNNOS = 2268
    CHAAC = 1966
    CHANGE = 1921
    CHARYBDIS = 4010
    CHERNOBOG = 3509
    CHIRON = 2075
    CHRONOS = 1920
    CLIODHNA = 4017
    CTHULHU = 3945
    CU_CHULAINN = 2319
    CUPID = 1778
    DA_JI = 2270
    DANZABUROU = 3984
    DISCORDIA = 3377
    ERLANG_SHEN = 2138
    ESET = 1918
    FAFNIR = 2136
    FENRIR = 1843
    FREYA = 1784
    GANESHA = 2269
    GEB = 1978
    GILGAMESH = 3997
    GUAN_YU = 1763
    HACHIMAN = 3344
    HADES = 1676
    HE_BO = 1674
    HEIMDALLR = 3812
    HEL = 1718
    HERA = 3558
    HERCULES = 1848
    HORUS = 3611
    HOU_YI = 2040
    HUN_BATZ = 1673
    ISHTAR = 4137
    IZANAMI = 2179
    JANUS = 1999
    JING_WEI = 2122
    JORMUNGANDR = 3585
    KALI = 1649
    KHEPRI = 2066
    KING_ARTHUR = 3565
    KUKULKAN = 1677
    KUMBHAKARNA = 1993
    KUZENBO = 2260
    LANCELOT = 4075
    LOKI = 1797
    MAUI = 4183
    MEDUSA = 2051
    MERCURY = 1941
    MERLIN = 3566
    MORGAN_LE_FAY = 4006
    MULAN = 3881
    NE_ZHA = 1915
    NEITH = 1872
    NEMESIS = 1980
    NIKE = 2214
    NOX = 2036
    NU_WA = 1958
    ODIN = 1669
    OLORUN = 3664
    OSIRIS = 2000
    PELE = 3543
    PERSEPHONE = 3705
    POSEIDON = 1881
    RA = 1698
    RAIJIN = 2113
    RAMA = 2002
    RATATOSKR = 2063
    RAVANA = 2065
    SCYLLA = 1988
    SERQET = 2005
    SET = 3612
    SHIVA = 4039
    SKADI = 2107
    SOBEK = 1747
    SOL = 2074
    SUN_WUKONG = 1944
    SUSANO = 2123
    SYLVANUS = 2030
    TERRA = 2147
    THANATOS = 1943
    THE_MORRIGAN = 2226
    THOR = 1779
    THOTH = 2203
    TIAMAT = 3990
    TSUKUYOMI = 3954
    TYR = 1924
    ULLR = 1991
    VAMANA = 1723
    VULCAN = 1869
    XBALANQUE = 1864
    XING_TIAN = 2072
    YEMOJA = 3811
    YMIR = 1670
    YU_HUANG = 4060
    ZEUS = 1672
    ZHONG_KUI = 1926

    @classmethod
    def has_value(self, value):
        # pylint: disable=no-member
        return value in self._value2member_map_

class GodPro(Enum):
    GREAT_JUNGLER = 'great jungler'
    HIGH_AREA_DAMAGE = 'high area damage'
    HIGH_ATTACK_SPEED = 'high attack speed'
    HIGH_CROWD_CONTROL = 'high crowd control'
    HIGH_DEFENSE = 'high defense'
    HIGH_MOBILITY = 'high mobility'
    HIGH_MOVEMENT_SPEED = 'high movement speed'
    HIGH_SINGLE_TARGET_DAMAGE = 'high single target damage'
    HIGH_SUSTAIN = 'high sustain'
    MEDIUM_AREA_DAMAGE = 'medium area damage'
    MEDIUM_CROWD_CONTROL = 'medium crowd control'
    PUSHER = 'pusher'

class GodRange(Enum):
    MELEE = 'melee'
    RANGED = 'ranged'

    @classmethod
    def has_value(self, value):
        # pylint: disable=no-member
        return value in self._value2member_map_

class GodRole(Enum):
    ASSASSIN = 'assassin'
    GUARDIAN = 'guardian'
    HUNTER = 'hunter'
    MAGE = 'mage'
    WARRIOR = 'warrior'
 
    @classmethod
    def has_value(self, value):
        # pylint: disable=no-member
        return value in self._value2member_map_

class GodType(Enum):
    MAGICAL = 'magical'
    PHYSICAL = 'physical'

    @classmethod
    def has_value(self, value):
        # pylint: disable=no-member
        return value in self._value2member_map_
