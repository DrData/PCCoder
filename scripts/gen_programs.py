import argparse
import random
import collections
import itertools
import json
from pathos.helpers import mp as multiprocessing

from dsl import constraint
from dsl.types import INT, LIST
from dsl.program import Program, get_used_indices, get_unused_indices
from dsl.impl import ALL_FUNCTIONS, LAMBDAS
from dsl.example import Example
from env.statement import Statement
import params

# For length 1 and 2, the number of possible programs in our DSL is not large and it is likely
# that a larger number will be used as args.num_train. We thus replace it with the known values
# from here when needed.
KNOWN_TRAIN_SIZES = {1: 47, 2: 2883}


def get_free_indices(program, program_len):
    """
    Returns unused indices for the given program
    """
    used = get_used_indices(program)
    total = set(range(program_len + len(program.input_types)))
    return total - used


def get_input_type_combinations(num_inputs):
    """
    Returns all possible input type combinations (list,int) for the given amount of inputs
    """
    input_type_combinations = []
    for num_inputs in range(1, num_inputs + 1):
        # no valid program takes only ints.
        for num_list in range(1, num_inputs + 1):
            input_types = [LIST] * num_list + [INT] * (num_inputs - num_list)
            input_type_combinations.append(input_types)
    return input_type_combinations


def iterate_inputs(function, type_to_vars):
    """
    Yields the cartesian product over all possible parameters to function based on type_to_vars
    """
    if isinstance(function.input_type, tuple):
        input_types = list(function.input_type)
    else:
        input_types = [function.input_type]

    argslists = []
    for input_type in input_types:
        argslists.append(type_to_vars[input_type])
    for args in itertools.product(*argslists):
        yield args


def init_gen_prog_worker(*args):
    global progress_counter, num_programs, program_len
    progress_counter, num_programs, program_len = args


def init_gen_examples_worker(*args):
    global progress_counter, valid_counter, num_programs, num_examples, num_example_tries
    progress_counter, valid_counter, num_programs, num_examples, num_example_tries = args


def gen_program_worker(input_types):
    """
    Generate programs with the given input types.
    Statements are generated by choosing a function randomly, and then sampling parameters so that
    unused variables take precedence. Programs that has unused variables are discarded.
    """
    def helper(functions, program, programs):
        random.shuffle(functions)
        if progress_counter.value >= num_programs:
            return True

        if len(program) >= program_len:
            if get_unused_indices(program) or program in programs:
                return False
            else:
                programs.add(program)
                progress_counter.value += 1
                print("\rGenerating programs... %d\\%d" % (progress_counter.value, num_programs), end="")
                return True

        type_to_vars = collections.defaultdict(list)
        for i, typ in enumerate(program.var_types):
            type_to_vars[typ].insert(0, i)

        # Move free indices to the front
        free_indxs = get_free_indices(program, program_len)
        for typ in program.var_types:
            for var in type_to_vars[typ]:
                if var in free_indxs:
                    type_to_vars[typ].remove(var)
                    type_to_vars[typ].insert(0, var)

        for func in LAMBDAS:
            type_to_vars[func.type].append(func)

        used = set(program.statements)
        for function in functions:
            for args in iterate_inputs(function, type_to_vars):
                if len([arg for arg in args if arg in free_indxs]) == 0:
                    continue
                statement = Statement(function, args)
                if statement in used:
                    continue

                next_program = Program(program.input_types,
                                       program.statements + [statement])
                if helper(functions, next_program, programs):
                    return True

    program_base = Program(input_types, [])
    res = set()
    while progress_counter.value < num_programs:
        helper(ALL_FUNCTIONS, program_base, res)
    return res


def gen_examples_worker(program):
    """
    Generate examples for the given program. Return the examples if successful, or None otherwise.
    """
    print("\rGenerating examples... %d\\%d (remaining programs: %d)" %
          (progress_counter.value, num_programs, valid_counter.value), end="")

    input_output_examples = constraint.get_input_output_examples(program, num_examples=num_examples,
                                                                 num_tries=num_example_tries)

    progress_counter.value += 1
    if input_output_examples:
        return input_output_examples
    else:
        valid_counter.value -= 1
        return None


def write_programs_to_file(f, programs, examples):
    for program in list(programs):
        raw_examples = []
        for inputs, output in examples[program]:
            raw_inputs = [x.val for x in inputs]
            raw_output = output.val
            raw_examples.append((raw_inputs, raw_output))

        program_examples = [dict(inputs=x[0], output=x[1]) for x in raw_examples]
        data = dict(program=program.encoded, examples=program_examples)
        f.write(json.dumps(data) + '\n')


def gen_programs(program_len, num_programs, args):
    """
    Generates the specified amount of programs of the given length. These are the exact steps performed:
    1. Generate <num_programs> programs using gen_program_worker in a process pool
    2. Generate examples for each program by executing gen_examples_worker in a process pool.
       Discard programs for which the required amount of examples could not be generated.
    3. Return a dictionary of the form {program: examples}
    """
    progress_counter = multiprocessing.Value('i', 0)
    gen_prog_pool = multiprocessing.Pool(processes=args.num_workers, initializer=init_gen_prog_worker,
                                         initargs=(progress_counter, num_programs, program_len))

    input_type_combinations = get_input_type_combinations(params.num_inputs)
    programs = gen_prog_pool.map(gen_program_worker, input_type_combinations)
    print('')

    # Flatten
    programs = [item for sublist in programs for item in sublist]
    programs = list(set(programs))

    # Generate examples and filter out null programs
    progress_counter.value = 0
    valid_counter = multiprocessing.Value('i', len(programs))
    gen_examples_pool = multiprocessing.Pool(processes=args.num_workers, initializer=init_gen_examples_worker,
                                             initargs=(progress_counter, valid_counter, len(programs),
                                                       args.num_examples, args.num_example_tries))

    res = gen_examples_pool.map(gen_examples_worker, programs)
    print('')
    examples = dict(zip(programs, res))
    examples = {k: v for k, v in examples.items() if v}
    return examples


def load_cache(path):
    """
    Given a dataset path, loads the programs from it to a form returned by gen_programs(): A dict with
    programs as keys and examples as values
    """
    lines = [json.loads(x) for x in open(path, 'r').readlines()]
    examples = {}
    for i, line in enumerate(lines):
        print("\rLoading program cache... %d\\%d" % (i, len(lines)), end="")
        program = Program.parse(line['program'])
        p_examples = Example.from_line(line)
        p_examples = [(ex.inputs, ex.output) for ex in p_examples]
        examples[program] = p_examples
    print('')

    return examples


def init_discard_identical_worker(*args):
    global existing_programs, progress_counter, new_program_count
    existing_programs, progress_counter, new_program_count = args


def discard_identical_worker(new_examples):
    """
    Given a dictionary of {program: examples}, and a current dataset (given via init_discard_identical_worker),
    this function deletes programs which are equivalent to any program in the current dataset.
    Equivalence is measured by using the examples from new_examples
    """
    new_programs = list(new_examples.keys())
    for i, program in enumerate(new_programs):
        for other in existing_programs:
            if constraint.is_same(program, other, new_examples[program]):
                del new_examples[program]
                break
        print("\rDiscarding identical programs... %d\\%d" % (progress_counter.value, new_program_count), end="")
        progress_counter.value += 1
    return new_examples


def main():
    """
    Generates programs. These are the basic steps performed:

    D = {}
    for 1 <= i <= max_train_len:
       1. P = Generate programs of length i
       2. E = Generate examples for the generated programs
       3. Discard programs in P that are equivalent to any program in D
       4. D += (P, E)

    for j in test_lengths:
      Sample num_test programs
      Discard all programs of equal length in D which are equivalent.

    Note:
        1. Step 3 of the first greatly increases the richness of the dataset. We ensure this way that
           our programs aren't likely to have shorter equivalents.
        2. It is recommended to use --cache to load a dataset cache. The algorithm then continues generating
           for lengths larger than the maximum length of the cache. This allows incremental dataset generation and
           also helps with the generation of shorter programs where generation is slow due to randomness. Furthermore,
           we can (and should!) have virtually all programs of length <=3, to ensure our dataset is meaningful.
        3. During test sampling we only compare to programs of equivalent lengths for efficiency. This is since
           our data generation algorithm already ensures that for all longer and shorter programs there is no
           equivalence.
        4. Since the pruning is done after program generation, rather than during, the number of programs generated
           in each iteration is NOT args.num_train. This is done purely due to implementation details: it is
           challenging to discard whilst generating since it would require all processes to write and read from
           the same dictionary in parallel. However, this is a good feature for the future, to avoid having to
           try multiple values for num_train via trial-and-error.
    """
    parser = argparse.ArgumentParser()

    parser.add_argument('--num_train', type=int, required=True)
    parser.add_argument('--num_test', type=int, required=True)
    parser.add_argument('--train_output_path', type=str, required=True)
    parser.add_argument('--test_output_path', type=str, required=True)
    parser.add_argument('--max_train_len', type=int, required=True)
    parser.add_argument('--test_lengths', type=str, required=True,
                        help="List of test lengths to generate")
    parser.add_argument('--num_workers', type=int, default=8)
    parser.add_argument('--num_examples', type=int, default=params.num_examples)
    parser.add_argument('--num_example_tries', type=int, default=200,
                        help='total amount of tries to generate examples to try to generate')
    parser.add_argument('--cache', type=str, default=None,
                        help="Dataset cache from which to continue generating programs")
    args = parser.parse_args()

    test_lens = set([int(x) for x in args.test_lengths.split()])

    if args.cache:
        examples = load_cache(args.cache)
        min_len = max([len(k) for k in examples])
    else:
        examples = {}
        min_len = 0

    for program_len in range(min_len + 1, args.max_train_len + 1):
        num_programs = args.num_train + args.num_test
        if program_len in KNOWN_TRAIN_SIZES:
            num_programs = min(num_programs, KNOWN_TRAIN_SIZES[program_len])

        print("Generating programs of length %d (current dataset size: %d)" % (program_len, len(examples)))
        new_examples = gen_programs(program_len, num_programs, args)

        existing_programs = list(examples.keys())
        counter = multiprocessing.Value('i', 0)
        new_programs = list(new_examples.keys())
        discard_pool = multiprocessing.Pool(processes=args.num_workers, initializer=init_discard_identical_worker,
                                            initargs=(existing_programs, counter, len(new_programs)))
        new_program_parts = [new_programs[i::args.num_workers] for i in range(args.num_workers)]

        new_example_parts = [{p: new_examples[p] for p in programs} for programs in new_program_parts]
        res = discard_pool.map(discard_identical_worker, new_example_parts)
        print('')
        for d in res:
            examples.update(d)

    train_programs = list(examples.keys())
    print("Finished generation. Total programs: %d" % len(train_programs))

    # Generate test programs (they're not equivalent to all shorter programs so only same length needs to be considered)
    for test_len in test_lens:
        test_programs = []
        test_candidates = [x for x in train_programs if len(x.statements) == test_len]
        train_programs = [x for x in train_programs if len(x.statements) != test_len]

        random.shuffle(test_candidates)
        indices_to_discard = set()
        for i, program in enumerate(test_candidates):
            if len(test_programs) >= args.num_test:
                break
            if i in indices_to_discard:
                continue

            print("\rCreating test programs for length %d... %d\\%d" % (test_len, len(test_programs), args.num_test),
                  end="")

            test_programs.append(program)
            indices_to_discard.add(i)

            for j, other in enumerate(test_candidates[i+1:]):
                if j in indices_to_discard:
                    continue
                if constraint.is_same(program, other, examples[program]):
                    indices_to_discard.add(j)
        print('')

        print("Removed %d programs" % len(indices_to_discard))
        train_programs += [test_candidates[i] for i in range(len(test_candidates)) if i not in indices_to_discard]

        output_path = args.test_output_path + '_' + str(test_len)
        print('Writing %d test programs to %s' % (len(test_programs), output_path))
        with open(output_path, 'w') as f:
            write_programs_to_file(f, test_programs, examples)

    print('Writing %d train programs to %s' % (len(train_programs), args.train_output_path))
    with open(args.train_output_path, 'w') as f:
        write_programs_to_file(f, train_programs, examples)


if __name__ == '__main__':
    main()
