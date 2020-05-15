from sys import argv
from Bubble_move import *

def main():
    if(len(argv) >= 2):
        param_test = My_Parameters(argv[1])
    else:
        param_test = My_Parameters("test.cfg")
    param_handler = param_test.get_param()
    try:
        print(str(param_handler["Reynolds_number"]))
        print(param_handler["Stabilization_Type"])
    except RuntimeError as e:
        print(str(e) +  "\nPlease check configuration file")

    sim = BubbleMove("test.cfg")
    sim.run()


if __name__ == "__main__":
    main()
