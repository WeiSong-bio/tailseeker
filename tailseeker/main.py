#!/usr/bin/env python3
#
# Copyright (c) 2013-2015 Institute for Basic Science
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
# - Hyeshik Chang <hyeshik@snu.ac.kr>
#

import os
import shutil

def get_topdir():
    if os.path.islink('Snakefile'):
        tailseekerpkgdir = os.path.dirname(os.readlink('Snakefile'))
        return os.path.abspath(os.path.dirname(tailseekerpkgdir))
    elif 'TAILSEEKER_DIR' in os.environ:
        return os.environ['TAILSEEKER_DIR']
    else:
        raise ValueError("You need to set an environment variable, TAILSEEKER_DIR.")

TAILSEEKER_DIR = get_topdir()
TARGETS = []

include: os.path.join(TAILSEEKER_DIR, 'tailseeker', 'snakesupport.py')

from tailseeker import sequencers

# Variable settings
TILES = sequencers.get_tiles(CONF)

EXP_SAMPLES = sorted(CONF['experimental_samples'].keys())
SPIKEIN_SAMPLES = sorted(CONF['spikein_samples'].keys())
ALL_SAMPLES = sorted(EXP_SAMPLES + SPIKEIN_SAMPLES)

INSERT_READS = sorted(readname for readname in CONF['read_cycles'] if readname[1] != 'i')
INDEX_READS = sorted(readname for readname in CONF['read_cycles'] if readname[1] == 'i')
ALL_READS = INSERT_READS + INDEX_READS

FIRST_CYCLE = min(f for f, l, _ in CONF['read_cycles'].values())
LAST_CYCLE = max(l for f, l, _ in CONF['read_cycles'].values())
NUM_CYCLES = LAST_CYCLE - FIRST_CYCLE + 1

PHIX_ID_REF = ['R5', 6, 40] # identify PhiX reads using 40 bases from the 6th cycle.

THREADS_MAXIMUM_CORE = CONF['maximum_threads']

INTERMEDIATE_DIRS = [
    'dupfilter', 'polya', 'scores', 'scratch',
    'sequences', 'signalproc', 'learning', 'tables',
]


# Variable validations
if len(INDEX_READS) != 1:
    raise ValueError("Multi-indexing is not supported yet.")

if FIRST_CYCLE != 1:
    raise ValueError("The pipeline assumes that one of the reads starts from the first cycle.")


def sorted_spikein_first(samples):
    spikein_lengths = CONF['spikein_lengths']

    names_with_design_length = [
        (spikein_lengths.get(sample, inf), sample)
        for sample in samples]

    return [name for _, name in sorted(names_with_design_length)]


localrules: all

rule all:
    input: lambda wc: TARGETS


def clear_generated_files(include_targets=False):
    cleared_files = 0
    for pattern in all_intermediate_files:
        from snakemake.utils import listfiles

        for filename, wildcards in listfiles(pattern):
            if os.path.isfile(filename):
                os.unlink(filename)
                cleared_files += 1
    else:
        if cleared_files > 0:
            print('{} intermediate file(s) were removed.'.format(cleared_files))

    for dir in INTERMEDIATE_DIRS:
        if os.path.islink(dir):
            realpath = os.readlink(dir)
            print('Cleaning a symbolic link:', dir)
            shutil.rmtree(realpath, ignore_errors=True)
            os.unlink(dir)
        elif os.path.isdir(dir):
            print('Cleaning an intermediate directory:', dir)
            shutil.rmtree(dir, ignore_errors=True)

    if not include_targets:
        return

    from itertools import groupby

    files_safe_to_delete = {'.AppleDouble', '.DS_Store'}

    for dirname, files in groupby(sorted(TARGETS), lambda x: x.split(os.sep)[0]):
        for f in files:
            if os.path.exists(f):
                os.unlink(f)

        if os.path.isdir(dirname) and not (
                set(os.listdir(dirname)) - files_safe_to_delete):
            shutil.rmtree(dirname)

rule clean:
    run:
        clear_generated_files(include_targets=False)

rule clear:
    run:
        clear_generated_files(include_targets=True)


rule basecall_ayb:
    """
    Runs a third-party basecaller, AYB, to call bases. In most cases, the sequence reads
    determined by AYB for 5' end of inserts are more readily aligned to the genome than
    performing that using Illumina RTA.

    Download my modified tree from https://github.com/hyeshik/AYB2. There is few bug fixes
    that were not incorporated into the original version.
    """
    output: temp('scratch/aybcalls/{read}_{tile}.fastq.gz')
    threads: THREADS_MAXIMUM_CORE
    run:
        tileinfo = TILES[int(wildcards.tile)]
        readname = wildcards.read
        first_cycle, last_cycle, read_no = CONF['read_cycles'][readname]
        read_length = last_cycle - first_cycle + 1

        tempdir = make_scratch_dir('aybcalls/{}_{}'.format(readname, tileinfo['id']))
        reads_format = (('' if first_cycle == 1 else 'I{}'.format(first_cycle - 1)) +
                        'R{}'.format(read_length))

        shell('{AYB_CMD} -p {threads} -o {tempdir} -i {tileinfo[datadir]} \
                -b {reads_format} -f fastq.gz -r L{tileinfo[lane]}T{tileinfo[tile]}')
        shell('mv {tempdir}/s_{tileinfo[lane]}_{tileinfo[tile]}.fastq.gz {output}')
        shutil.rmtree(tempdir)


def determine_inputs_demultiplex_signals(wildcards, as_dict=False):
    inputs = {}

    for readid, program in CONF.get('third_party_basecaller', {}).items():
        fastqpath = 'scratch/{program}calls/{readid}_{tile}.fastq.gz'.format(
                        program=program.lower(), readid=readid, tile=wildcards.tile)
        inputs[readid] = fastqpath

    return inputs if as_dict else list(inputs.values())

rule demultiplex_signals:
    """
    Extract signals and basecalls from .CIF, .BCL, and FASTQ files. The sequence and signals
    are sorted into files in the in-house .SQI format after demultiplexing to separate
    reads by their indices (barcodes).
    """
    input: determine_inputs_demultiplex_signals
    output:
        regular = map(temp, expand('tables/primary_sqi/{sample}_{{tile}}.h5',
                                   sample=ALL_SAMPLES)),
        unknown = temp('tables/primary_sqi/Unknown_{tile}.h5'),
        phixcontrol = temp('tables/primary_sqi/PhiX_{tile}.h5'),
        demuxstats = temp('stats/demultiplexing-{tile}.csv')
    threads: min(len(EXP_SAMPLES) + 2, 8)
    run:
        tileinfo = TILES[int(wildcards.tile)]
        index_read_start, index_read_end, read_no = CONF['read_cycles'][INDEX_READS[0]]
        index_read_length = index_read_end - index_read_start + 1

        phix_cycle_base = CONF['read_cycles'][PHIX_ID_REF[0]][0]
        phix_cycle_start = phix_cycle_base + PHIX_ID_REF[1] - 1
        phix_cycle_length = PHIX_ID_REF[2]

        writer_command = (
            '{PYTHON3_CMD} {SCRIPTSDIR}/write-sqi-to-table.py '
            '--output tables/primary_sqi/_SAMPLE__{wildcards.tile}.h5 '
            '--tileid {wildcards.tile} --samplename XX')

        options = [
            '--data-dir', tileinfo['intensitiesdir'],
            '--run-id', tileinfo['laneid'], '--lane', tileinfo['lane'],
            '--tile', tileinfo['tile'], '--ncycles', NUM_CYCLES,
            '--signal-scale', sequencers.get_signalscale(tileinfo['type']),
            '--barcode-start', index_read_start, '--barcode-length', index_read_length,
            '--writer-command', writer_command,
            '--filter-control', 'PhiX,{},{}'.format(phix_cycle_start, phix_cycle_length),
            '--demultiplex-stats', output.demuxstats,
        ]

        altcalls = determine_inputs_demultiplex_signals(wildcards, as_dict=True)
        for readid, inputfile in altcalls.items():
            options.extend([
                '--alternative-call',
                '{},{}'.format(inputfile, CONF['read_cycles'][readid][0])])

        for sample in ALL_SAMPLES:
            options.extend([
                '--sample',
                '{name},{index},{index_mm},{delimiter},{delimpos},{delim_mm}'.format(
                    name=sample, index=CONF.get_sample_index(sample),
                    index_mm=CONF['maximum_index_mismatches'][sample],
                    delimiter=CONF['delimiter'][sample][1],
                    delimpos=CONF['delimiter'][sample][0],
                    delim_mm=CONF['delimiter'][sample][2])])

        shell('{BINDIR}/tailseq-retrieve-signals ' +
              ' '.join('"{}"'.format(opt) for opt in options))


rule index_tabix:
    input: '{name}.gz'
    output: nonfinal('{name}.gz.tbi')
    shell: '{TABIX_CMD} -s1 -b2 -e2 -0 {input}'


rule filter_sequence_reads:
    input:
        expand('tables/primary_sqi/{{sample}}_{tile}.h5', tile=sorted(TILES))
    output: dupstats=temp('scratch/stats/{sample}.duplicates.csv'), \
            filtstats='stats/{sample}.filters.csv', \
            uniqids=nonfinal('tables/uniqids/{sample}.h5'), \
            uniqsqi=nonfinal('tables/sqi/{sample}.h5')
    params: tileids=sorted(TILES)
    threads: THREADS_MAXIMUM_CORE
    run:
        sample = wildcards.sample
        umiopt = balanceopt = fingerprintopt = ''

        if sample in CONF['umi_fixed_sequence']:
            umiopt = ('--umi-sequence {} --umi-position {} '
                      '--umi-mismatch 1').format(CONF['umi_fixed_sequence'][sample],
                                                 CONF['read_cycles']['R3'][0])

        if sample in CONF['balance_check']:
            balanceopt = ('--balance-region {}:{} --balance-minimum {}').format(*
                            CONF['balance_check'][sample])

        if wildcards.sample in CONF['dupcheck_regions']:
            checkregions = CONF['dupcheck_regions'][wildcards.sample]
            fingerprintopt = (
                ' '.join('--fingerprint-region {}:{}'.format(begin, end)
                         for begin, end in checkregions) +
                ' --output-duplicity-stats {output.dupstats}'.format(output=output))
        else:
            # Touch this file to statisfy the snakemake's output file checks
            open(output.dupstats, 'w')

        sqifiles = ['{}:{}'.format(tileid, filename)
                    for tileid, filename in zip(params.tileids, input)]

        shell('{PYTHON3_CMD} {SCRIPTSDIR}/filter-sequence-reads.py \
                --sample-name {wildcards.sample} \
                --output-filter-stats {output.filtstats} \
                --output-uniqids {output.uniqids} \
                --output-uniq-sqi {output.uniqsqi} \
                --parallel {threads} \
                {fingerprintopt} {umiopt} {balanceopt} \
                {sqifiles}')


TARGETS.extend(expand('stats/{sample}.duplicates.csv', sample=EXP_SAMPLES))
rule copy_duplicate_count_stats:
    input: 'scratch/stats/{sample}.duplicates.csv'
    output: 'stats/{sample}.duplicates.csv'
    shell: 'cp {input} {output}'


rule collect_color_matrices:
    output: nonfinal('signalproc/colormatrix.pickle')
    run:
        external_script('{PYTHON3_CMD} {SCRIPTSDIR}/collect-color-matrices.py', ['TILES'])


TARGETS.extend(['stats/signal-scaling-references.csv',
                'stats/signal-scaling-badcycles.csv',])
rule calculate_phix_signal_scaling_factor:
    input:
        signals=expand('tables/primary_sqi/PhiX_{tile}.h5', tile=sorted(TILES)),
        colormatrix='signalproc/colormatrix.pickle'
    output:
        paramout=nonfinal('tables/signalproc/phix.h5'),
        refcntstats_out='stats/signal-scaling-references.csv',
        badcycles_out='stats/signal-scaling-badcycles.csv'
    params: tileids=sorted(TILES)
    threads: THREADS_MAXIMUM_CORE
    run:
        readstart, readend, readno = CONF['read_cycles']['R3']
        spotnormlen = 20 # Use first N cycles for detection of spot brightness

        external_script('{PYTHON3_CMD} {SCRIPTSDIR}/prepare-phix-signal-scaler.py',
                        ['readstart', 'readend', 'readno', 'spotnormlen'])


rule prepare_signal_stabilizer:
    input:
        colormatrix='signalproc/colormatrix.pickle',
        signals='tables/sqi/{sample}.h5',
        globalscaling='tables/signalproc/phix.h5'
    output: nonfinal('tables/signalproc/sample_{sample}.h5')
    threads: THREADS_MAXIMUM_CORE
    run:
        balancer_length = CONF['umi_length'][wildcards.sample]
        normalizer_conf = CONF['persample_signal_normalizer']
        read_cycles = CONF['read_cycles']

        external_script('{PYTHON3_CMD} {SCRIPTSDIR}/prepare-signal-stabilizer.py',
                        ['balancer_length', 'normalizer_conf', 'read_cycles'])

        if 0:
            balancer_length = CONF['umi_length'][wildcards.sample]
            high_probe_range = '{}:{}'.format(
                    balancer_length + SIGNAL_STABILIZER_POLYA_DETECTION_RANGE[0],
                    balancer_length + SIGNAL_STABILIZER_POLYA_DETECTION_RANGE[1])
            high_probe_scale_inspection = '{}:{}'.format(
                    balancer_length + SIGNAL_STABILIZER_TARGET_RANGE[0],
                    balancer_length + SIGNAL_STABILIZER_TARGET_RANGE[1])
            high_probe_scale_basis = '{}:{}'.format(
                    balancer_length + SIGNAL_STABILIZER_REFERENCE_RANGE[0],
                    balancer_length + SIGNAL_STABILIZER_REFERENCE_RANGE[1])

            cyclestart, cycleend, readno = CONF['read_cycles']['R3']
            shell('{PYTHON3_CMD} {SCRIPTSDIR}/prepare-signal-stabilizer.py \
                    --parallel {threads} --output {output} \
                    --read {readno} --color-matrix {input.colormatrix} \
                    --cycle-scaling {input.cyclescaling} \
                    --high-probe-range {high_probe_range} \
                    --high-probe-scale-inspection {high_probe_scale_inspection} \
                    --high-probe-scale-basis {high_probe_scale_basis} \
                    --read-range {cyclestart}:{cycleend} --spot-norm-length {balancer_length} \
                    {input.signals}')


def determine_inputs_calc_pasignals_v2(wildcards):
    sample = wildcards.sample
    stabilizer_ref = sample if sample in EXP_SAMPLES else CONF['spikein_scaling_ref']

    return ['signalproc/colormatrix.pickle',
            'sequences/{sample}.sqi.gz'.format(sample=sample),
            'sequences/{sample}.sqi.gz.tbi'.format(sample=sample),
            'signalproc/signal-scaling.phix-ref.pickle',
            'signalproc/signal-scaling-{sample}.stabilizer.pickle'.format(
                sample=stabilizer_ref)]


TARGETS.extend(expand('stats/{sample}.pascore-calculation.csv', sample=ALL_SAMPLES))
rule calculate_pasignals_v2:
    input: determine_inputs_calc_pasignals_v2
    output:
        pascore=nonfinal('scores/{sample}.pa2score.gz'),
        stats='stats/{sample}.pascore-calculation.csv'
    threads: THREADS_MAXIMUM_CORE
    run:
        input = suffix_filter(input)
        shell('{PYTHON3_CMD} {SCRIPTSDIR}/calculate-pasignals.py --parallel {threads} \
                --output-stats {output.stats} \
                --scaling-params {input[stabilizer.pickle]} {input[sqi.gz]} \
                > {output.pascore}')


TARGETS.extend(expand('qcplots/{sample}.trainer.pdf', sample=CONF['spikeins_to_learn']))
rule pick_spikein_samples_for_training:
    input: 'scores/{sample}.pa2score.gz'
    output:
        result=nonfinal('learning/{sample}.trainer.npy'),
        qcplot='qcplots/{sample}.trainer.pdf',
        idlist=nonfinal('learning/{sample}.trainer.idlist')
    run:
        trim_len = CONF['spikein_training_length'][wildcards.sample]
        samples_to_learn = CONF['spikein_learning_num_samples']

        shell('{PYTHON3_CMD} {SCRIPTSDIR}/pick-samples-to-learn.py --input-pascore {input} \
                --output {output.result} --output-qc-plot {output.qcplot} \
                --qc-plot-range 0:2 \
                --pass1 {samples_to_learn[pass1]} \
                --pass2 {samples_to_learn[pass2]} \
                --support-fraction 0.75 --contamination 0.4 \
                --granule-size 15 --trim {trim_len} \
                --output-training-set-list {output.idlist}')


rule learn_pascores_from_spikeins:
    input: expand('learning/{sample}.trainer.npy', sample=CONF['spikeins_to_learn'])
    output: nonfinal('learning/model.pickle')
    shell: '{PYTHON3_CMD} {SCRIPTSDIR}/learn-spikein-pa-score.py \
                --preset v2 \
                --clip-minimum {PASIGNAL_CLIP_MIN} --clip-maximum {PASIGNAL_CLIP_MAX} \
                --output {output} {input}'


rule measure_polya:
    input:
        sqi='sequences/{sample}.sqi.gz',
        sqiindex='sequences/{sample}.sqi.gz.tbi',
        score='scores/{sample}.pa2score.gz',
        scoreinex='scores/{sample}.pa2score.gz.tbi',
        model='learning/model.pickle'
    output: nonfinal('polya/{sample}.polya-calls.gz')
    threads: THREADS_MAXIMUM_CORE
    shell: '{PYTHON3_CMD} {SCRIPTSDIR}/measure-polya-tails.py \
                --input-sqi {input.sqi} --input-pa {input.score} \
                --model {input.model} --parallel {threads} --output {output}'


TARGETS.extend(expand('fastq/{sample}_{readno}.fastq.gz',
                      sample=EXP_SAMPLES, readno=INSERT_READS))
rule generate_fastq:
    input:
        sqi='sequences/{sample}.sqi.gz',
        sqiindex='sequences/{sample}.sqi.gz.tbi',
        pacall='polya/{sample}.polya-calls.gz',
        pacallindex='polya/{sample}.polya-calls.gz.tbi'
    output: expand('fastq/{{sample}}_{readno}.fastq.gz', readno=INSERT_READS)
    params: output='fastq/{sample}_XX.fastq.gz'
    threads: THREADS_MAXIMUM_CORE
    run:
        reads = ''
        for readname in INSERT_READS:
            start, end, _ = CONF['read_cycles'][readname]
            if wildcards.sample not in CONF['delimiter']:
                trim = 'N'
            elif start <= CONF['delimiter'][wildcards.sample][0] <= end:
                delimend = (CONF['delimiter'][wildcards.sample][0] +
                            len(CONF['delimiter'][wildcards.sample][1]))
                trim = end - (delimend + MAXIMUM_DELIMITER_MISALIGNMENT - 1)
            else:
                trim = 'N'

            reads += ' {},{},{},{}'.format(readname, start, end, trim)

        shell('{PYTHON3_CMD} {SCRIPTSDIR}/generate-fastq.py \
                --input-sqi {input.sqi} --input-pa-call {input.pacall} \
                --parallel {threads} --output {params.output} {reads}')


TARGETS.extend(['stats/control-length-accuracy.csv'] +
               expand('qcplots/control-length-accuracy-{plottype}.pdf',
                      plottype=['final', 'seqbased', 'hmmbased', 'naive']))
rule generate_accuracy_stats:
    input: expand('polya/{sample}.polya-calls.gz', sample=SPIKEIN_SAMPLES)
    output:
        statsout='stats/control-length-accuracy.csv',
        plotsout=expand('qcplots/control-length-accuracy-{plottype}.pdf',
                        plottype=['final', 'seqbased', 'hmmbased', 'naive'])
    params: plotdir='qcplots'
    run:
        controlsamples = ' '.join(
            '{}:polya/{}.polya-calls.gz'.format(CONF['spikein_lengths'][s], s)
            for s in SPIKEIN_SAMPLES)

        shell('{PYTHON3_CMD} {SCRIPTSDIR}/plot-polya-calls-accuracy.py \
                    --control {controlsamples} --output-plots {params.plotdir} \
                    --output-stats {output.statsout}')


TARGETS.append('stats/polya-length-distributions.csv')
rule generate_polya_length_distribution_stats:
    input: expand('polya/{sample}.polya-calls.gz', sample=sorted_spikein_first(ALL_SAMPLES))
    output: 'stats/polya-length-distributions.csv'
    params:
        samplenames=sorted_spikein_first(ALL_SAMPLES),
        maxpalength=CONF['read_cycles']['R3'][1]
    run:
        external_script('{PYTHON3_CMD} {SCRIPTSDIR}/stats-polya-len-dists.py')

TARGETS.append('qcplots/global-polya-length.pdf')
rule plot_global_polya_length_distributions:
    input: 'stats/polya-length-distributions.csv'
    output: 'qcplots/global-polya-length.pdf'
    params:
        controls=','.join(name for length, name
                          in sorted((CONF['spikein_lengths'][s], s) for s in SPIKEIN_SAMPLES)
                          if length > 0),
        samples=','.join(EXP_SAMPLES)
    shell: '{PYTHON3_CMD} {SCRIPTSDIR}/plot-virtual-gel.py \
                    --tagcounts {input} --controls {params.controls} \
                    --samples {params.samples} --output-plot {output}'

TARGETS.append('stats/demultiplexing.csv')
rule merge_demultiplexing_stats:
    input: expand('stats/demultiplexing-{tile}.csv', tile=sorted(TILES))
    output: 'stats/demultiplexing.csv'
    params: tiles=[TILES[tileno] for tileno in sorted(TILES)]
    run:
        external_script('{PYTHON3_CMD} {SCRIPTSDIR}/stats-merge-demultiplexing-counts.py')

# ex: syntax=snakemake
