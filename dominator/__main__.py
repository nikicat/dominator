import logging
import argh

from .actions import dump, run, containers


def main():
    parser = argh.ArghParser()
    parser.add_argument('-l', dest='loglevel', default=logging.INFO, type=int, help='log level')
    parser.add_commands([dump, run, containers])
    args = parser.parse_args()
    logging.basicConfig(level=args.loglevel)
    parser.dispatch()


if __name__ == '__main__':
    main()
