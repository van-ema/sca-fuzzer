"""
File: x86-specific model implementation

Copyright (C) Microsoft Corporation
SPDX-License-Identifier: MIT
"""
import re
import numpy as np
from typing import Tuple, Dict, List, Set
from copy import copy

import unicorn.x86_const as ucc
from unicorn import Uc, UC_MEM_WRITE, UC_ARCH_X86, UC_MODE_64

from interfaces import Input, FlagsOperand, RegisterOperand, MemoryOperand
from model import UnicornModel, UnicornSpec, UnicornSeq, UnicornBpas, BaseTaintTracker
from x86.x86_target_desc import X86UnicornTargetDesc, X86TargetDesc
from service import UnreachableCode

FLAGS_CF = 0b000000000001
FLAGS_PF = 0b000000000100
FLAGS_AF = 0b000000010000
FLAGS_ZF = 0b000001000000
FLAGS_SF = 0b000010000000
FLAGS_OF = 0b100000000000


class X86UnicornModel(UnicornModel):
    """
    Base class that serves as main interface.
    Loads inputs and executes the test case on x86
    """

    def __init__(self, sandbox_base, code_start):
        self.target_desc = X86UnicornTargetDesc()
        self.architecture = (UC_ARCH_X86, UC_MODE_64)
        super().__init__(sandbox_base, code_start)

    def _load_input(self, input_: Input):
        """
        Set registers and stack before starting the emulation
        """
        # Set memory:
        # - initialize overflows with zeroes
        self.emulator.mem_write(self.lower_overflow_base, self.overflow_region_values)
        self.emulator.mem_write(self.upper_overflow_base, self.overflow_region_values)

        # - sandbox pages
        self.emulator.mem_write(self.sandbox_base, input_.get_memory().tobytes())

        # Set values in registers
        regs = self.target_desc.registers
        flags = self.target_desc.flags_register
        reg_init_address = self.sandbox_base + self.MAIN_REGION_SIZE + self.FAULTY_REGION_SIZE
        for i, value in enumerate(input_.get_registers()):
            if regs[i] == flags:
                value = (value & np.uint64(2263)) | np.uint64(2)  # type: ignore
            self.emulator.reg_write(regs[i], value)

            # executor uses the lower bytes of the upper_overflow_region to initialize registers
            # we need to match it in the model
            self.emulator.mem_write(reg_init_address, value.tobytes())
            reg_init_address += 8
        self.emulator.mem_write(reg_init_address,
                                self.stack_base.to_bytes(8, byteorder='little', signed=False))

        self.emulator.reg_write(ucc.UC_X86_REG_RSP, self.stack_base)
        self.emulator.reg_write(ucc.UC_X86_REG_RBP, self.stack_base)
        self.emulator.reg_write(ucc.UC_X86_REG_R14, self.sandbox_base)

    def print_state(self, oneline: bool = False):

        def compressed(val: int):
            if val >= self.sandbox_base and val <= self.sandbox_base + 12288:
                return f"+0x{val - self.sandbox_base:<15x}"
            elif val >= self.sandbox_base - self.OVERFLOW_REGION_SIZE and val < self.sandbox_base:
                return f"+0x{val - self.sandbox_base:<15x}"
            else:
                return f"0x{val:<15x}"

        emulator = self.emulator
        rax = compressed(emulator.reg_read(ucc.UC_X86_REG_RAX))
        rbx = compressed(emulator.reg_read(ucc.UC_X86_REG_RBX))
        rcx = compressed(emulator.reg_read(ucc.UC_X86_REG_RCX))
        rdx = compressed(emulator.reg_read(ucc.UC_X86_REG_RDX))
        rsi = compressed(emulator.reg_read(ucc.UC_X86_REG_RSI))
        rdi = compressed(emulator.reg_read(ucc.UC_X86_REG_RDI))

        if not oneline:
            print("\n\nRegisters:")
            print(f"RAX: {rax}")
            print(f"RBX: {rbx}")
            print(f"RCX: {rcx}")
            print(f"RDX: {rdx}")
            print(f"RSI: {rsi}")
            print(f"RDI: {rdi}")
        else:
            print(f"  rax={rax} "
                  f"rbx={rbx} "
                  f"rcx={rcx} \n"
                  f"  rdx={rdx} "
                  f"rsi={rsi} "
                  f"rdi={rdi} \n"
                  f"  fl={emulator.reg_read(ucc.UC_X86_REG_EFLAGS):012b}")


# ==================================================================================================
# Implementation of Execution Clauses
# ==================================================================================================
class X86UnicornSeq(UnicornSeq, X86UnicornModel):
    pass


class X86UnicornSpec(UnicornSpec, X86UnicornModel):
    pass


class X86UnicornCond(X86UnicornSpec):
    """
    Contract for conditional branch mispredicitons.
    Forces all cond. branches to speculatively go into a wrong target
    """

    jumps = {
        # c - the byte code of the instruction
        # f - the value of EFLAGS
        0x70: lambda c, f, r: (c[1:], f & FLAGS_OF != 0, False),  # JO
        0x71: lambda c, f, r: (c[1:], f & FLAGS_OF == 0, False),  # JNO
        0x72: lambda c, f, r: (c[1:], f & FLAGS_CF != 0, False),  # JB
        0x73: lambda c, f, r: (c[1:], f & FLAGS_CF == 0, False),  # JAE
        0x74: lambda c, f, r: (c[1:], f & FLAGS_ZF != 0, False),  # JZ
        0x75: lambda c, f, r: (c[1:], f & FLAGS_ZF == 0, False),  # JNZ
        0x76: lambda c, f, r: (c[1:], f & FLAGS_CF != 0 or f & FLAGS_ZF != 0, False),  # JNA
        0x77: lambda c, f, r: (c[1:], f & FLAGS_CF == 0 and f & FLAGS_ZF == 0, False),  # JNBE
        0x78: lambda c, f, r: (c[1:], f & FLAGS_SF != 0, False),  # JS
        0x79: lambda c, f, r: (c[1:], f & FLAGS_SF == 0, False),  # JNS
        0x7A: lambda c, f, r: (c[1:], f & FLAGS_PF != 0, False),  # JP
        0x7B: lambda c, f, r: (c[1:], f & FLAGS_PF == 0, False),  # JPO
        0x7C: lambda c, f, r: (c[1:], (f & FLAGS_SF == 0) != (f & FLAGS_OF == 0), False),  # JNGE
        0x7D: lambda c, f, r: (c[1:], (f & FLAGS_SF == 0) == (f & FLAGS_OF == 0), False),  # JNL
        0x7E: lambda c, f, r:
        (c[1:], f & FLAGS_ZF != 0 or (f & FLAGS_SF == 0) != (f & FLAGS_OF == 0), False),
        0x7F: lambda c, f, r:
        (c[1:], f & FLAGS_ZF == 0 and (f & FLAGS_SF == 0) == (f & FLAGS_OF == 0), False),
        0xE0: lambda c, f, r: (c[1:], r != 1 and (f & FLAGS_ZF == 0), True),  # LOOPNE
        0xE1: lambda c, f, r: (c[1:], r != 1 and (f & FLAGS_ZF != 0), True),  # LOOPE
        0xE2: lambda c, f, r: (c[1:], r != 1, True),  # LOOP
        0xE3: lambda c, f, r: (c[1:], r == 0, False),  # J*CXZ
        0x0F: lambda c, f, r:
        X86UnicornCond.multibyte_jmp.get(c[1], (lambda _, __, ___: ([0], False, False)))(c, f, r)
    }  # yapf: disable

    multibyte_jmp: Dict = {
        0x80: lambda c, f, r: (c[2:], f & FLAGS_OF != 0, False),  # JO
        0x81: lambda c, f, r: (c[2:], f & FLAGS_OF == 0, False),  # JNO
        0x82: lambda c, f, r: (c[2:], f & FLAGS_CF != 0, False),  # JB
        0x83: lambda c, f, r: (c[2:], f & FLAGS_CF == 0, False),  # JAE
        0x84: lambda c, f, r: (c[2:], f & FLAGS_ZF != 0, False),  # JE
        0x85: lambda c, f, r: (c[2:], f & FLAGS_ZF == 0, False),  # JNE
        0x86: lambda c, f, r: (c[2:], f & FLAGS_CF != 0 or f & FLAGS_ZF != 0, False),  # JBE
        0x87: lambda c, f, r: (c[2:], f & FLAGS_CF == 0 and f & FLAGS_ZF == 0, False),  # JA
        0x88: lambda c, f, r: (c[2:], f & FLAGS_SF != 0, False),  # JS
        0x89: lambda c, f, r: (c[2:], f & FLAGS_SF == 0, False),  # JNS
        0x8A: lambda c, f, r: (c[2:], f & FLAGS_PF != 0, False),  # JP
        0x8B: lambda c, f, r: (c[2:], f & FLAGS_PF == 0, False),  # JPO
        0x8C: lambda c, f, r: (c[2:], (f & FLAGS_SF == 0) != (f & FLAGS_OF == 0), False),  # JNGE
        0x8D: lambda c, f, r: (c[2:], (f & FLAGS_SF == 0) == (f & FLAGS_OF == 0), False),  # JNL
        0x8E: lambda c, f, r:
        (c[2:], f & FLAGS_ZF != 0 or (f & FLAGS_SF == 0) != (f & FLAGS_OF == 0), False),
        0x8F: lambda c, f, r:
        (c[2:], f & FLAGS_ZF == 0 and (f & FLAGS_SF == 0) == (f & FLAGS_OF == 0), False),
    }  # yapf: disable

    @staticmethod
    def speculate_instruction(emulator: Uc, address, size, model: UnicornModel) -> None:
        # reached max spec. window? skip
        if len(model.checkpoints) >= model.nesting:
            return

        # if the instruction is undefined, Unicorn will return a huge value as size
        # skip those
        if size > 15:  # 15 bytes is max instr size on Intel
            return

        # decode the instruction
        code = emulator.mem_read(address, size)
        flags = emulator.reg_read(ucc.UC_X86_REG_EFLAGS)
        rcx = emulator.reg_read(ucc.UC_X86_REG_RCX)
        target, will_jump, is_loop = X86UnicornCond.decode(code, flags, rcx)

        # not a a cond. jump? ignore
        if not target:
            return

        # LOOP instructions must also decrement RCX
        if is_loop:
            emulator.reg_write(ucc.UC_X86_REG_RCX, rcx - 1)

        # Take a checkpoint
        next_instr = address + size + target if will_jump else address + size
        model.checkpoint(emulator, next_instr)

        # Simulate misprediction
        if will_jump:
            emulator.reg_write(ucc.UC_X86_REG_RIP, address + size)
        else:
            emulator.reg_write(ucc.UC_X86_REG_RIP, address + size + target)

    @staticmethod
    def decode(code: bytearray, flags: int, rcx: int) -> Tuple[int, bool, bool]:
        """
        Decodes the instruction encoded in `code` and, if it's a conditional jump,
        returns its expected target, whether it will jump to the target (based
        on the `flags` value), and whether it is a LOOP instruction
        """
        calculate_target = X86UnicornCond.jumps.get(code[0], (lambda _, __, ___:
                                                              ([0], False, False)))
        target, will_jump, is_loop = calculate_target(code, flags, rcx)
        if len(target) == 1:
            return target[0], will_jump, is_loop
        return int.from_bytes(target, byteorder='little'), will_jump, is_loop

    @staticmethod
    def speculate_mem_access(emulator, access, address, size, value, model):
        pass  # cond does not need to speculate mem accesses


class X86UnicornBpas(UnicornBpas, X86UnicornModel):
    pass


class X86UnicornNull(X86UnicornSpec):
    instruction_address: int
    relevant_faults: List[int]
    injection_pending: bool = False

    def __init__(self, *args):
        super().__init__(*args)
        self.relevant_faults = [12, 13]

    def speculate_fault(self, errno: int) -> int:
        if errno not in self.relevant_faults:  # we speculate only on a subset of faults
            return 0

        # reached max spec. window? skip
        if len(self.checkpoints) >= self.nesting:
            return 0

        # tell speculate_mem_access that we need to inject zero
        self.injection_pending = True

        # remove protection
        self.emulator.mem_protect(self.sandbox_base + self.MAIN_REGION_SIZE,
                                  self.FAULTY_REGION_SIZE)

        # re-execute the instruction
        return self.instruction_address

    @staticmethod
    def speculate_instruction(emulator: Uc, address, size, model) -> None:
        assert isinstance(model, X86UnicornNull)
        # store the address for checkpointing (see speculate_fault)
        model.instruction_address = address

    @staticmethod
    def speculate_mem_access(emulator, access, address, size, value, model):
        assert isinstance(model, X86UnicornNull)

        if not model.injection_pending:
            return
        model.injection_pending = False

        # applicable only to loads
        if access == UC_MEM_WRITE:
            return

        # store a checkpoint
        model.checkpoint(emulator, model.instruction_address)
        model.store_logs[-1].append((address, emulator.mem_read(address, 8)))

        # emulate zero-injection by writing zero to the target address of the load
        zero_value = bytes([0 for _ in range(size)])
        emulator.mem_write(address, zero_value)


class X86UnicornNullFault(X86UnicornNull):

    @staticmethod
    def speculate_mem_access(emulator, access, address, size, value, model):
        assert isinstance(model, X86UnicornNull)

        if not model.injection_pending:
            return
        model.injection_pending = False

        # applicable only to loads
        if access == UC_MEM_WRITE:
            return

        # store a checkpoint
        model.checkpoint(emulator, model.code_end)
        model.store_logs[-1].append((address, emulator.mem_read(address, 8)))

        # emulate zero-injection by writing zero to the target address of the load
        zero_value = bytes([0 for _ in range(size)])
        emulator.mem_write(address, zero_value)


class X86UnicornCondBpas(X86UnicornSpec):

    @staticmethod
    def speculate_mem_access(emulator, access, address, size, value, model: UnicornSpec):
        X86UnicornBpas.speculate_mem_access(emulator, access, address, size, value, model)

    @staticmethod
    def speculate_instruction(emulator: Uc, address, size, model) -> None:
        X86UnicornCond.speculate_instruction(emulator, address, size, model)
        X86UnicornBpas.speculate_instruction(emulator, address, size, model)


class x86UnicornOOO(X86UnicornSpec):
    """
    Contract for out-of-order handling of faults
    """
    relevant_faults: List[int]
    next_instr_address: int
    dependencies: Set[str]
    dependency_checkpoints: List[Set[str]]

    def __init__(self, *args):
        super().__init__(*args)
        self.relevant_faults = [12, 13]
        self.dependencies = set()
        self.dependency_checkpoints = []

    def speculate_fault(self, errno: int) -> int:
        if errno not in self.relevant_faults:  # we speculate only on a subset of faults
            return 0

        # reached max spec. window? skip
        if len(self.checkpoints) >= self.nesting:
            return 0

        # start speculation
        # we set the rollback address to the end of the testcase
        # because faults are terminating execution
        self.checkpoint(self.emulator, self.code_end)

        # add destinations to the dependency list
        for op in self.current_instruction.get_dest_operands(True):
            if isinstance(op, RegisterOperand):
                self.dependencies.add(X86TargetDesc.gpr_normalized[op.value])
            elif isinstance(op, FlagsOperand):
                for flag in op.get_write_flags():
                    self.dependencies.add(flag)

        # speculatively skip the faulting instruction
        if self.next_instr_address >= self.code_end:
            return 0  # no need for speculation if we're at the end
        else:
            return self.next_instr_address

    @staticmethod
    def speculate_instruction(emulator: Uc, address, size, model) -> None:
        """
        Track instruction dependencies to skip those instructions that are dependent
        on a faulting instruction
        """
        assert isinstance(model, x86UnicornOOO)

        # store the address for checkpointing (see speculate_fault)
        model.next_instr_address = address + size

<<<<<<< HEAD
        # track dependencies only after faults
        if not model.in_speculation or not model.dependencies:
=======
        if len(model.dependencies) == 0:
            return

        if model.last_faulty_instr_address == model.curr_instr_address:
>>>>>>> 747c2bd (x86/model: contract for GP faults)
            return

        # check if the instruction should be skipped due to a dependency on a faulting instr
        reg_src_operands = []
        reg_dest_operands = []
        for op in model.current_instruction.get_all_operands():
            if isinstance(op, RegisterOperand):
                if op.src:
                    reg_src_operands.append(X86TargetDesc.gpr_normalized[op.value])
                if op.dest:
                    reg_dest_operands.append(X86TargetDesc.gpr_normalized[op.value])
            elif isinstance(op, MemoryOperand):
                for sub_op in re.split(r'\+|-|\*| ', op.value):
                    if sub_op and sub_op in X86TargetDesc.gpr_normalized:
                        reg_src_operands.append(X86TargetDesc.gpr_normalized[sub_op])
            elif isinstance(op, FlagsOperand):
                reg_src_operands.extend(op.get_read_flags())
                reg_dest_operands.extend(op.get_write_flags())

        is_dependent = False
        for reg in reg_src_operands:
            if reg in model.dependencies:
                is_dependent = True
                break

        # remove overwritten values from dependencies
        for reg in reg_dest_operands:
            if reg not in reg_src_operands and reg in model.dependencies:
                model.dependencies.remove(reg)

        if not is_dependent:
            return

        # update dependencies
        for reg in reg_dest_operands:
            model.dependencies.add(reg)

        # skip the dependent instruction
        emulator.reg_write(ucc.UC_X86_REG_RIP, address + size)

<<<<<<< HEAD
=======
    @staticmethod
    def trace_mem_access(emulator, access, address, size, value, model) -> None:
        assert isinstance(model, x86UnicornOOO)

        # speculate only on the accesses to the faulty page
        if (address >= model.faulty_region
            and address < model.faulty_region + model.FAULTY_REGION_SIZE)\
                and not model.in_speculation and UC_ERR_EXCEPTION in model.ooo_faults:
            model.pending_fault = UC_ERR_EXCEPTION  # 21
            model.last_faulty_instr_address = \
                model.curr_instr_address
            emulator.emu_stop()
            return

        X86UnicornSpec.trace_mem_access(emulator, access, address, size, value, model)

>>>>>>> 747c2bd (x86/model: contract for GP faults)
    def checkpoint(self, emulator: Uc, next_instruction):
        self.dependency_checkpoints.append(copy.copy(self.dependencies))
        return super().checkpoint(emulator, next_instruction)

    def rollback(self) -> int:
        self.dependencies = self.dependency_checkpoints.pop()
        return super().rollback()


class X86UnicornDivOverflow(x86UnicornOOO):
    div_value: int = 0

    def speculate_fault(self, errno: int) -> int:
        if self.current_instruction.name not in ["DIV", "IDIV"]:
            return super().speculate_fault(errno)

        # reached max spec. window? skip
        if len(self.checkpoints) >= self.nesting:
            return 0

        assert len(self.current_instruction.operands) == 1
        assert self.current_instruction.operands[0].src
        divider = self.current_instruction.operands[0]
        if isinstance(divider, RegisterOperand):
            uc_id = X86UnicornTargetDesc.reg_str_to_constant[divider.value]
            value = self.emulator.reg_read(uc_id)
        elif isinstance(divider, MemoryOperand):
            value = self.div_value
        else:
            raise UnreachableCode()

        # skip div by zero exceptions
        if value == 0:
            return super().speculate_fault(errno)

        # start speculation
        self.checkpoint(self.emulator, self.code_end)

        if self.current_instruction.name == "DIV":
            # set carry flag
            # flags = self.emulator.reg_read(ucc.UC_X86_REG_EFLAGS)
            # self.emulator.reg_write(ucc.UC_X86_REG_EFLAGS, flags | FLAGS_CF)

            # execute division with trimming
            width = divider.width
            if width == 64:
                a = self.emulator.reg_read(ucc.UC_X86_REG_RAX)
                d = self.emulator.reg_read(ucc.UC_X86_REG_RDX)
                trimmed_result = (((d << 64) + a) // value) % 0xffffffffffffffff
                self.emulator.reg_write(ucc.UC_X86_REG_RAX, trimmed_result)
                self.emulator.reg_write(ucc.UC_X86_REG_RDX, ((d << 64) + a) % value)
                return self.next_instr_address
            if width == 32:
                a = self.emulator.reg_read(ucc.UC_X86_REG_EAX)
                d = self.emulator.reg_read(ucc.UC_X86_REG_EDX)
                trimmed_result = (((d << 32) + a) // value) % 0xffffffff
                # self.emulator.reg_write(ucc.UC_X86_REG_RAX, 0)
                # self.emulator.reg_write(ucc.UC_X86_REG_RDX, 0)
                self.emulator.reg_write(ucc.UC_X86_REG_EAX, trimmed_result)
                self.emulator.reg_write(ucc.UC_X86_REG_EDX, ((d << 32) + a) % value)
                return self.next_instr_address
            if width == 16:
                a = self.emulator.reg_read(ucc.UC_X86_REG_AX)
                d = self.emulator.reg_read(ucc.UC_X86_REG_DX)
                trimmed_result = (((d << 16) + a) // value) % 0xffff
                # self.emulator.reg_write(ucc.UC_X86_REG_RAX, 0)
                # self.emulator.reg_write(ucc.UC_X86_REG_RDX, 0)
                self.emulator.reg_write(ucc.UC_X86_REG_AX, trimmed_result)
                self.emulator.reg_write(ucc.UC_X86_REG_DX, ((d << 16) + a) % value)
                return self.next_instr_address
            if width == 8:
                a = self.emulator.reg_read(ucc.UC_X86_REG_AX)
                trimmed_result = (a // value) % 0xff
                # self.emulator.reg_write(ucc.UC_X86_REG_RAX, 0)
                # self.emulator.reg_write(ucc.UC_X86_REG_RDX, 0)
                self.emulator.reg_write(ucc.UC_X86_REG_AL, trimmed_result)
                self.emulator.reg_write(ucc.UC_X86_REG_AH, a % value)
                # trimmed_result = 1024
                # self.emulator.reg_write(ucc.UC_X86_REG_AX, trimmed_result)
                return self.next_instr_address
            raise UnreachableCode()
        else:  # IDIV
            raise UnreachableCode()

    @staticmethod
    def trace_mem_access(emulator: Uc, access, address: int, size, value, model):
        model.div_value = int.from_bytes(emulator.mem_read(address, size), "little")
        x86UnicornOOO.trace_mem_access(emulator, access, address, size, value, model)


class X86MeltdownModel(x86UnicornOOO):
    """
     Loads from the faulty region speculatively return the in-memory value
    """

    @staticmethod
    def speculate_instruction(emulator: Uc, address, size, model) -> None:
        """ Do nothing - assume we speculate the invalid access """
        model.next_instr_address = address + size
        model.curr_instr_address = address


class X86GPOOOModel(x86UnicornOOO):
    """
     Trace faulty and out-of-order instructions but skip data dependent ones
    """

    def speculate_fault(self, errno: int) -> int:
        '''
         Recover from faulty instruction after the address has been corrected
        (see speculate_instruction)
        '''
        ret = super().speculate_fault(errno)
        if errno == 6 and ret != 0:
            return self.curr_instr_address  # We re-execute but this time with canonica address
        return ret

    @staticmethod
    def speculate_instruction(emulator: Uc, address, size, model) -> None:
        assert isinstance(model, x86UnicornOOO)
        # store the address for checkpointing (see speculate_fault)
        model.next_instr_address = address + size
        model.curr_instr_address = address

        # We detect access to non-canonical addresses before unicorn triggers the exception,
        # otherwise the context is corrupted and unicorn keeps faulting.
        curr: Instruction = model.current_instruction
        for mem_op in curr.get_mem_operands():
            registers = re.split(r'\+|-|\*| ', mem_op.value)
            if len(registers) > 1:
                continue
            uc_reg = model.target_desc.reg_str_to_constant[registers[0]]
            low = 0x00007fffffffffff
            high = 0xffff800000000000
            old = model.emulator.reg_read(uc_reg)
            if old > low and old < high:
                model.pending_fault = 6  # We simualte the fault
                model.last_faulty_instr_address = address
                reg_new = old ^ 0x1000000000000  # mask used to make the address non-canonical
                model.emulator.reg_write(uc_reg, reg_new)
                # Snapshot already taken in trace_instruction
                # but we need to update the context
                # otherwise the exception is raised again
                model.previous_context = model.emulator.context_save()
                emulator.emu_stop()
                return  # Continue execution with canonical address

        if not model.in_speculation and not model.dependencies:
            return

        if len(model.dependencies) == 0:
            return

        if model.last_faulty_instr_address == model.curr_instr_address:
            return

        # check if the instruction should be skipped due to a dependency on a faulting instr
        reg_src_operands = []
        reg_dest_operands = []
        for op in model.current_instruction.get_all_operands():
            if isinstance(op, RegisterOperand):
                if op.src:
                    reg_src_operands.append(X86TargetDesc.gpr_normalized[op.value])
                if op.dest:
                    reg_dest_operands.append(X86TargetDesc.gpr_normalized[op.value])
            elif isinstance(op, MemoryOperand):
                for sub_op in re.split(r'\+|-|\*| ', op.value):
                    if sub_op and sub_op in X86TargetDesc.gpr_normalized:
                        reg_src_operands.append(X86TargetDesc.gpr_normalized[sub_op])
            elif isinstance(op, FlagsOperand):
                reg_src_operands.extend(op.get_read_flags())
                reg_dest_operands.extend(op.get_write_flags())

        is_dependent = False
        for reg in reg_src_operands:
            if reg in model.dependencies:
                is_dependent = True
                break

        # remove overwritten values from dependencies
        for reg in reg_dest_operands:
            if reg not in reg_src_operands and reg in model.dependencies:
                model.dependencies.remove(reg)

        if not is_dependent:
            return

        # instruction is dependent but we can execute the memory access
        all_operands = model.current_instruction.get_mem_operands()
        implicit = model.current_instruction.get_implicit_mem_operands()
        is_dependent = False
        for op in all_operands:
            if op in implicit:
                continue
            for sub_op in re.split(r'\+|-|\*| ', op.value):
                if sub_op and sub_op in X86TargetDesc.gpr_normalized:
                    if X86TargetDesc.gpr_normalized[sub_op] in model.dependencies:
                        is_dependent = True
                        break

        if not is_dependent:
            return

        # update dependencies
        for reg in reg_dest_operands:
            model.dependencies.add(reg)

        emulator.reg_write(ucc.UC_X86_REG_RIP, address + size)


# ==================================================================================================
# Taint tracker
# ==================================================================================================
class X86TaintTracker(BaseTaintTracker):
    # ISA-specific fields
    _registers = [
        ucc.UC_X86_REG_RAX, ucc.UC_X86_REG_RBX, ucc.UC_X86_REG_RCX, ucc.UC_X86_REG_RDX,
        ucc.UC_X86_REG_RSI, ucc.UC_X86_REG_RDI, ucc.UC_X86_REG_EFLAGS
    ]

    def __init__(self, initial_observations, sandbox_base=0):
        super().__init__(initial_observations, sandbox_base=sandbox_base)

        # ISA-specific field setup
        self.target_desc = X86TargetDesc()
        self.unicorn_target_desc = X86UnicornTargetDesc()
